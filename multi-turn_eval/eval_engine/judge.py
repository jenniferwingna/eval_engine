"""判分引擎：给一条跑完的用例判定 pass/fail。

覆盖范围（诚实标注，不假装比实际做得多）：
- ⑤ LLM 二元判分：全自动。几乎每条用例都填了「二元判分问题」+「期望答案」，
  这是本引擎里唯一对全部 17 个维度都通用的判分路径。
- ③ 禁止动作：只对能从文本里解析出「api(args)」或「调用次数」这类机器可读
  形式的条目做代码核查；纯语义描述（比如「根据任一儿童的存在猜测左右」）
  代码判不了，会显式标为 needs_manual，不会悄悄判过。
- ①④ 终态断言 / 结构化信息断言：只在「结构化信息断言」或「期望终态」里能
  解析出明确的 api + 参数时才做代码核查（覆盖 CLARIFY / INFERMEM / INSTKEEP /
  VERSION 这类形状）；FALSEMEM/SELFCON 这种「不应该发生什么」的用例走
  「调用次数为 0 / write_action_count 为 0」的专门分支。
- ⑥ 人工抽检：不自动化，只记录需要人工看的信号。

判定口径：pass = 判分输出 == 期望答案（不是恒等于 yes）；组合判分载体
（比如「③禁止动作+⑤二元问题」）用 AND：任一子机制 fail，整条 fail。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from canon import args_match, canonicalize_args, parse_func_call_string, parse_kv_string_list
from llm_client import ChatClient

YES_WORDS = {"yes", "是", "true", "1"}
NO_WORDS = {"no", "否", "false", "0"}


def _norm_answer(value: Any) -> str:
    text = str(value).strip().lower()
    if text in YES_WORDS:
        return "yes"
    if text in NO_WORDS:
        return "no"
    return text


@dataclass
class SubVerdict:
    mechanism: str
    status: str  # "pass" | "fail" | "needs_manual" | "not_applicable"
    detail: str


@dataclass
class CaseVerdict:
    case_id: str
    passed: bool | None = None  # None when it can't be fully automated
    sub_verdicts: list[SubVerdict] = field(default_factory=list)

    def add(self, mechanism: str, status: str, detail: str) -> None:
        self.sub_verdicts.append(SubVerdict(mechanism, status, detail))


def _tool_calls_in_turn_range(
    turns: list[dict[str, Any]], start: int | None, end: int | None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for t in turns:
        idx = t.get("turn_index")
        if start is not None and (idx is None or idx < start):
            continue
        if end is not None and (idx is None or idx > end):
            continue
        calls.extend(t.get("tool_calls") or [])
    return calls


def _check_turn_bounds(case: dict[str, Any]) -> tuple[int | None, int | None]:
    check = case.get("检查轮次")
    if isinstance(check, list) and check:
        return min(check), max(check)
    if isinstance(check, int):
        return check, check
    return None, None


def code_check_expected_call(case: dict[str, Any], turns: list[dict[str, Any]]) -> SubVerdict | None:
    """Best-effort structured/end-state check. Returns None if the case's
    fields don't carry a machine-parseable positive api+args spec."""
    start, end = _check_turn_bounds(case)
    candidates: list[dict[str, Any]] = []

    def add_candidate(api: Any, params: Any):
        if api and isinstance(params, dict):
            candidates.append({"api": api, "params": params})

    end_state = case.get("期望终态")
    if isinstance(end_state, dict) and "api" in end_state:
        add_candidate(end_state.get("api"), end_state.get("arguments"))

    sfa = case.get("结构化信息断言")
    if isinstance(sfa, dict):
        api = sfa.get("api") or sfa.get("API")
        params = sfa.get("parameters") or sfa.get("参数")
        add_candidate(api, params)
        # CLARIFY-style shape: 检查轮次前已知 + 补齐后 list-of-kv-strings.
        if "补齐后" in sfa or "检查轮次前已知" in sfa:
            known = parse_kv_string_list(sfa.get("检查轮次前已知"))
            filled = parse_kv_string_list(sfa.get("补齐后"))
            merged = {**known, **filled}
            api2 = end_state.get("api") if isinstance(end_state, dict) else None
            if merged and api2:
                add_candidate(api2, merged)
    elif isinstance(sfa, list):
        for item in sfa:
            if isinstance(item, dict) and ("api" in item):
                add_candidate(item.get("api"), item.get("参数") or item.get("parameters"))

    if not candidates:
        return None

    calls_in_range = _tool_calls_in_turn_range(turns, start, end)
    all_calls = _tool_calls_in_turn_range(turns, None, None)
    details = []
    all_ok = True
    for cand in candidates:
        expected_args = canonicalize_args(cand["params"])
        matches = [
            c for c in calls_in_range
            if c.get("name") == cand["api"] and args_match(expected_args, canonicalize_args(c.get("args") or {}))
        ]
        if matches:
            details.append(f"OK {cand['api']}({expected_args}) matched in check-turn range")
        else:
            any_call = [c for c in all_calls if c.get("name") == cand["api"]]
            all_ok = False
            details.append(
                f"MISSING {cand['api']}({expected_args}) — "
                f"{'api called elsewhere but args/turn mismatch: ' + str(any_call) if any_call else 'api never called'}"
            )
    return SubVerdict(
        mechanism="code:expected_call",
        status="pass" if all_ok else "fail",
        detail="; ".join(details),
    )


def code_check_zero_calls(case: dict[str, Any], turns: list[dict[str, Any]]) -> SubVerdict | None:
    """FALSEMEM/SELFCON-style: some api must NOT have been called (optionally
    only within a turn range), or a generic "write_action_count == 0"."""
    sfa = case.get("结构化信息断言")
    forbidden = case.get("禁止动作")
    targets: list[tuple[str, dict[str, str]]] = []

    if isinstance(forbidden, str):
        name, args = parse_func_call_string(forbidden)
        if name:
            targets.append((name, args))
    elif isinstance(forbidden, list):
        for item in forbidden:
            name, args = parse_func_call_string(item)
            if name:
                targets.append((name, args))

    zero_count_flag = False
    if isinstance(sfa, dict):
        for key, value in sfa.items():
            if ("调用次数" in key or "write_action_count" in key.lower()) and value == 0:
                zero_count_flag = True

    if not targets and not zero_count_flag:
        return None

    all_calls = _tool_calls_in_turn_range(turns, None, None)
    details = []
    all_ok = True
    for name, args in targets:
        expected_args = canonicalize_args(args)
        hits = [
            c for c in all_calls
            if c.get("name") == name and args_match(expected_args, canonicalize_args(c.get("args") or {}))
        ]
        if hits:
            all_ok = False
            details.append(f"FORBIDDEN CALL FOUND: {name}({expected_args}) x{len(hits)}")
        else:
            details.append(f"OK: {name}({expected_args}) never called")
    if zero_count_flag and not targets:
        # Can't name the exact api — just report whether ANY tool call happened
        # at all as a coarse proxy, flagged clearly as coarse.
        details.append(
            f"COARSE PROXY (no explicit api named): total tool calls in transcript = {len(all_calls)}"
        )
        if all_calls:
            all_ok = False
    return SubVerdict(
        mechanism="code:zero_calls",
        status="pass" if all_ok else "fail",
        detail="; ".join(details),
    )


def code_check_forbidden_free_text(case: dict[str, Any], turns: list[dict[str, Any]]) -> SubVerdict:
    """Anything in 禁止动作 that ISN'T a parseable api(args) call — report as
    needs_manual so it's visible rather than silently skipped."""
    forbidden = case.get("禁止动作")
    items = forbidden if isinstance(forbidden, list) else ([forbidden] if forbidden else [])
    unparsed = [i for i in items if not parse_func_call_string(i)[0]]
    if not unparsed:
        return SubVerdict("forbidden:free_text", "not_applicable", "no free-text forbidden clauses")
    return SubVerdict(
        "forbidden:free_text",
        "needs_manual",
        f"{len(unparsed)} forbidden clause(s) are semantic, not machine-checkable: {unparsed}",
    )


def _check_turn_transcript_slice(case: dict[str, Any], turns: list[dict[str, Any]]) -> str:
    start, end = _check_turn_bounds(case)
    lines = []
    for t in turns:
        idx = t.get("turn_index")
        if start is not None and end is not None and idx is not None and not (start <= idx <= end):
            continue
        if t.get("user_text"):
            lines.append(f"R{idx} 用户: {t['user_text']}")
        if t.get("assistant_reply"):
            lines.append(f"R{idx} agent: {t['assistant_reply']}")
        for call in t.get("tool_calls") or []:
            lines.append(f"R{idx} agent调用工具: {call.get('name')}({call.get('args')}) ok={call.get('ok')}")
    return "\n".join(lines) if lines else "(check-turn range produced no content)"


def llm_binary_judge(case: dict[str, Any], turns: list[dict[str, Any]], client: ChatClient | None = None) -> SubVerdict:
    question = case.get("二元判分问题")
    expected = _norm_answer(case.get("期望答案"))
    if not question or not expected:
        return SubVerdict("llm:binary", "not_applicable", "case has no 二元判分问题/期望答案")

    client = client or ChatClient()
    slice_text = _check_turn_transcript_slice(case, turns)
    prompt = (
        "你是多轮语音助手评测的判分员。只根据下面【检查轮次内容】判断问题，"
        "不要用你自己的常识脑补检查轮次之外发生了什么。只回答 yes 或 no，不要解释。\n\n"
        f"【检查轮次内容】\n{slice_text}\n\n"
        f"【判分问题】{question}\n"
        "只输出 yes 或 no。"
    )
    response = client.complete(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=5,
    )
    actual = _norm_answer(response.text)
    status = "pass" if actual == expected else "fail"
    return SubVerdict(
        "llm:binary",
        status,
        f"question={question!r} expected={expected} actual={actual} raw={response.text!r}",
    )


def judge_case(case: dict[str, Any], turns: list[dict[str, Any]], client: ChatClient | None = None) -> CaseVerdict:
    verdict = CaseVerdict(case_id=case.get("case_id", "?"))
    mechanism_label = str(case.get("判分载体", ""))

    for check_fn in (code_check_expected_call, code_check_zero_calls):
        result = check_fn(case, turns)
        if result is not None:
            verdict.sub_verdicts.append(result)

    verdict.sub_verdicts.append(code_check_forbidden_free_text(case, turns))
    verdict.sub_verdicts.append(llm_binary_judge(case, turns, client=client))

    statuses = [sv.status for sv in verdict.sub_verdicts]
    if "fail" in statuses:
        verdict.passed = False
    elif "needs_manual" in statuses:
        verdict.passed = None
    else:
        verdict.passed = True
    return verdict
