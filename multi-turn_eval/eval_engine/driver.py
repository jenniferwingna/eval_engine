#!/usr/bin/env python3
"""Drives real multi-turn eval cases against a running cockpit_harness
instance over its native WebSocket protocol, then judges them.

This is the "打通链条" proof: user-simulator -> live harness (SUT) ->
judge -> summary, using the harness's OWN wire protocol (the same one
harness/scripts/evaluate_text_calls.py and evaluate_memory_e2e.py already
use for single-turn / memory evals) — no adapter layer needed because the
harness's `user.text` / `tool.call.completed` / `state.snapshot` events
already carry what HARNESS_SUT_CONTRACT.md asked for.

Usage:
    python3 driver.py --case-id CLARIFY-001
    python3 driver.py --dimension CLARIFY --limit 3
    python3 driver.py --case-id CLARIFY-001 --provider mock   # plumbing-only, no LLM cost

Requires a running harness (see ../说明与工具/README_ENGINE.md):
    cd cockpit_harness && sh start_harness_mock.sh     # cheap plumbing check
    cd cockpit_harness && sh start_harness.sh          # real LLM (qwen3_omni default; connect with ?mode=text)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets

from case_schema import parse_turn_script
from judge import CaseVerdict, SubVerdict, judge_case
from llm_client import ChatClient
from simulator import build_user_turn

ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "dataset" / "multiturn_eval_set_approved.json"
RUNS_ROOT = ROOT / "eval_runs"

CONFIRMATION_DELTA_TYPES = {
    "response.audio_transcript.delta",
    "response.text.delta",
    "response.output_text.delta",
}


def load_dataset() -> dict[str, Any]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def find_cases(data: dict[str, Any], case_id: str = "", dimension: str = "", limit: int = 0) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for dim in data["dimensions"]:
        if dimension and dim["prefix"] != dimension:
            continue
        for item in dim["items"]:
            if case_id and item.get("case_id") != case_id:
                continue
            cases.append(item)
    if limit:
        cases = cases[:limit]
    return cases


async def recv_json(ws: Any, timeout: float) -> dict[str, Any]:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def send_event(ws: Any, typ: str, session_id: str, payload: dict[str, Any] | None = None) -> None:
    await ws.send(json.dumps({"type": typ, "payload": payload or {}, "session_id": session_id}, ensure_ascii=False))


async def wait_ready(ws: Any, timeout: float) -> str:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        evt = await recv_json(ws, max(0.1, deadline - time.perf_counter()))
        if evt.get("type") == "session.ready":
            return str(evt.get("session_id") or "")
    raise TimeoutError("session.ready timeout")


async def reset_vehicle(ws: Any, session_id: str, timeout: float) -> str:
    await send_event(ws, "vehicle_session.reset", session_id)
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        evt = await recv_json(ws, max(0.1, deadline - time.perf_counter()))
        if evt.get("type") == "session.ready":
            return str(evt.get("session_id") or session_id)
    return session_id


async def drain_short(ws: Any, seconds: float = 0.2) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        try:
            await recv_json(ws, max(0.01, deadline - time.perf_counter()))
        except asyncio.TimeoutError:
            break


async def send_and_collect(ws: Any, session_id: str, text: str, timeout: float) -> dict[str, Any]:
    """Send one user.text and collect the full turn response, working for
    both the mock/rule-based path (assistant.response.completed terminal)
    and the qwen tool-plan/confirmation path (response.done terminal,
    gated on the tool_confirmation prompt stage — same gating
    evaluate_text_calls.py uses)."""
    await send_event(ws, "user.text", session_id, {"text": text})

    staged = False  # True once we see ANY model.prompt.prepared (qwen path only)
    seen_confirmation_prompt = False
    tool_calls: list[dict[str, Any]] = []
    text_parts: list[str] = []
    mock_text = ""
    last_state: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = []
    timed_out = False
    deadline = time.perf_counter() + timeout

    while time.perf_counter() < deadline:
        try:
            evt = await recv_json(ws, max(0.1, deadline - time.perf_counter()))
        except asyncio.TimeoutError:
            timed_out = True
            break
        typ = evt.get("type")
        payload = evt.get("payload") or {}

        if typ == "model.prompt.prepared":
            staged = True
            if str(payload.get("stage") or "") == "tool_confirmation":
                seen_confirmation_prompt = True
        elif typ == "tool.call.completed":
            tool_calls.append(
                {
                    "name": payload.get("name"),
                    "args": payload.get("args") or {},
                    "ok": payload.get("ok"),
                    "message": payload.get("message"),
                }
            )
        elif typ == "state.snapshot":
            last_state = payload
        elif typ == "assistant.text.done":
            mock_text = str(payload.get("text") or "")
        elif typ in CONFIRMATION_DELTA_TYPES:
            if seen_confirmation_prompt:
                text_parts.append(str(payload.get("delta") or ""))
        elif typ in {"provider.warning", "error", "assistant.grounding.warning"}:
            warnings.append({"type": typ, "payload": payload})
        elif typ == "assistant.response.completed":
            if not staged or seen_confirmation_prompt:
                break
        elif typ == "response.done":
            if seen_confirmation_prompt:
                break

    assistant_reply = mock_text or "".join(text_parts).strip()
    return {
        "assistant_reply": assistant_reply,
        "tool_calls": tool_calls,
        "state": last_state,
        "warnings": warnings,
        "timeout": timed_out,
    }


async def run_case(
    ws: Any,
    session_id: str,
    case: dict[str, Any],
    sim_client: ChatClient,
    connect_timeout: float,
    turn_timeout: float,
    literal: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    session_id = await reset_vehicle(ws, session_id, connect_timeout)
    await drain_short(ws)

    mode, driven_turns = parse_turn_script(case)
    turns_log: dict[int, dict[str, Any]] = {}
    history: list[dict[str, str]] = []
    protocol_warnings: list[str] = []

    for dt in driven_turns:
        if literal:
            user_text = dt.goal
            sim_warnings: list[str] = ["literal mode: used 目标 text verbatim, not simulator-generated"]
        else:
            sim_result = build_user_turn(case, {"目标": dt.goal, "条件": dt.condition}, history, client=sim_client)
            user_text = sim_result.text
            sim_warnings = sim_result.warnings

        turns_log.setdefault(dt.turn_index, {"turn_index": dt.turn_index})
        turns_log[dt.turn_index]["user_text"] = user_text
        turns_log[dt.turn_index]["sim_goal"] = dt.goal
        turns_log[dt.turn_index]["sim_condition"] = dt.condition
        if sim_warnings:
            turns_log[dt.turn_index]["sim_warnings"] = sim_warnings
        history.append({"role": "user", "text": user_text})

        result = await send_and_collect(ws, session_id, user_text, turn_timeout)
        turns_log.setdefault(dt.assistant_turn_index, {"turn_index": dt.assistant_turn_index})
        turns_log[dt.assistant_turn_index]["assistant_reply"] = result["assistant_reply"]
        turns_log[dt.assistant_turn_index]["tool_calls"] = result["tool_calls"]
        turns_log[dt.assistant_turn_index]["state"] = result["state"]
        if result["warnings"]:
            turns_log[dt.assistant_turn_index]["harness_warnings"] = result["warnings"]
        if result["timeout"]:
            protocol_warnings.append(f"turn {dt.turn_index}->{dt.assistant_turn_index}: response timed out")
        history.append({"role": "assistant", "text": result["assistant_reply"]})

    ordered = [turns_log[k] for k in sorted(turns_log.keys())]
    protocol_warnings.insert(0, f"numbering_mode={mode}")
    return ordered, protocol_warnings


def verdict_to_dict(v: CaseVerdict) -> dict[str, Any]:
    return {
        "case_id": v.case_id,
        "passed": v.passed,
        "sub_verdicts": [asdict(sv) for sv in v.sub_verdicts],
    }


async def run(args: argparse.Namespace) -> int:
    data = load_dataset()
    cases = find_cases(data, case_id=args.case_id, dimension=args.dimension, limit=args.limit)
    if not cases:
        print("no matching cases", file=sys.stderr)
        return 2

    ws_url = args.ws_url
    if args.provider and "mode=" not in ws_url:
        sep = "&" if "?" in ws_url else "?"
        ws_url = f"{ws_url}{sep}mode={args.provider}"

    sim_client = None if args.literal else ChatClient()
    judge_client = ChatClient()

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else RUNS_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    cases_path = output_dir / "cases.jsonl"
    summary_path = output_dir / "summary.json"

    results: list[dict[str, Any]] = []
    async with websockets.connect(ws_url, proxy=None, max_size=16 * 1024 * 1024) as ws:
        session_id = await wait_ready(ws, args.connect_timeout)
        print(f"session={session_id} ws={ws_url} cases={len(cases)} output={output_dir}", flush=True)
        with cases_path.open("w", encoding="utf-8") as f:
            for idx, case in enumerate(cases, start=1):
                case_id = case.get("case_id", "?")
                started = time.perf_counter()
                try:
                    turns, protocol_warnings = await run_case(
                        ws, session_id, case, sim_client,
                        args.connect_timeout, args.turn_timeout, literal=args.literal,
                    )
                    verdict = judge_case(case, turns, client=judge_client) if not args.no_judge else None
                    record = {
                        "case_id": case_id,
                        "dimension": case.get("维度"),
                        "判分载体": case.get("判分载体"),
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                        "protocol_warnings": protocol_warnings,
                        "turns": turns,
                        "verdict": verdict_to_dict(verdict) if verdict else None,
                    }
                except Exception as exc:  # keep the run alive across one bad case
                    record = {
                        "case_id": case_id,
                        "dimension": case.get("维度"),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                results.append(record)
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                if "error" in record:
                    status = "ERROR"
                elif record.get("verdict") is None:
                    status = "OK(no-judge)"
                else:
                    status = {"True": "PASS", "False": "FAIL", "None": "MANUAL"}[str(record["verdict"]["passed"])]
                print(f"[{idx}/{len(cases)}] {status} {case_id} ({round((time.perf_counter()-started)*1000)}ms)", flush=True)

    def verdict_passed(r: dict[str, Any]):
        v = r.get("verdict")
        return v.get("passed") if v else None

    passed = sum(1 for r in results if verdict_passed(r) is True)
    failed = sum(1 for r in results if verdict_passed(r) is False)
    manual = sum(1 for r in results if r.get("verdict") is not None and verdict_passed(r) is None)
    errored = sum(1 for r in results if "error" in r)
    summary = {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "needs_manual": manual,
        "errored": errored,
        "ws_url": ws_url,
        "literal_mode": args.literal,
        "output_dir": str(output_dir),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run multiturn_eval_set_approved.json cases against a live cockpit_harness.")
    p.add_argument("--case-id", default="")
    p.add_argument("--dimension", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--ws-url", default="ws://127.0.0.1:8861/ws")
    p.add_argument("--provider", default="", choices=["", "mock", "text", "voice"], help="appends ?mode=... to ws-url")
    p.add_argument("--output-dir", default="")
    p.add_argument("--connect-timeout", type=float, default=35.0)
    p.add_argument("--turn-timeout", type=float, default=45.0)
    p.add_argument("--literal", action="store_true", help="send 轮次脚本's 目标 text verbatim instead of running the LLM simulator (cheap plumbing check)")
    p.add_argument("--no-judge", action="store_true", help="skip the LLM judge call (plumbing-only)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run(parse_args(argv or sys.argv[1:])))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"driver failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
