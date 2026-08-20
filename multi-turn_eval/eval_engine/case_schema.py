"""Normalizes the three different 轮次脚本 shapes found in
multiturn_eval_set_approved.json into one list of driveable user turns.

Observed shapes (see 说明与工具/multiturn_eval_guide.html for the full writeup):
  1. list[dict] with 角色 (CLARIFY): alternating 用户/助手 slots, each with its
     own turn number. Only 角色=="用户" entries are things we actually send;
     the 助手 entries are judging anchors, and the resulting reply gets
     logged under THAT entry's own 轮次 number (paired numbering).
  2. list[dict] without 角色, using 脚本目标/条件规则 (FALSEMEM): every entry
     is one user turn; the assistant's reaction is logged under the SAME
     turn number (same-turn numbering) — 检查轮次 refers to "what happened
     as a result of user turn N", not a separately numbered assistant slot.
  3. list[str] like "第7轮目标：..." / "R3 目标：..." (INFERMEM/INSTKEEP/
     SELFCON/VERSION): same-turn numbering, goal+condition already merged
     into one string.

Getting the numbering convention wrong silently makes 检查轮次/结构化信息断言
turn-range lookups miss the right transcript row, so this is worth its own
module instead of inlining a guess into the driver.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TURN_NUM_RE = re.compile(r"第\s*(\d+)\s*轮|R\s*(\d+)")


@dataclass
class DrivenTurn:
    turn_index: int          # number to log the user_text under
    assistant_turn_index: int  # number to log the resulting assistant_reply/tool_calls under
    goal: str
    condition: str


def _extract_turn_num(text: str, fallback: int) -> int:
    m = TURN_NUM_RE.search(text or "")
    if not m:
        return fallback
    return int(m.group(1) or m.group(2))


def parse_turn_script(case: dict) -> tuple[str, list[DrivenTurn]]:
    """Returns (numbering_mode, driven_turns). numbering_mode is "paired" or
    "same", exposed mainly for logging/debugging."""
    script = case.get("轮次脚本")
    if not isinstance(script, list) or not script:
        return "unknown", []

    if isinstance(script[0], dict) and "角色" in script[0]:
        driven: list[DrivenTurn] = []
        for i, item in enumerate(script):
            if item.get("角色") != "用户":
                continue
            turn_index = int(item.get("轮次"))
            assistant_turn_index = turn_index + 1
            if i + 1 < len(script) and isinstance(script[i + 1], dict):
                nxt = script[i + 1].get("轮次")
                if nxt is not None:
                    assistant_turn_index = int(nxt)
            driven.append(
                DrivenTurn(
                    turn_index=turn_index,
                    assistant_turn_index=assistant_turn_index,
                    goal=str(item.get("目标", "")),
                    condition=str(item.get("条件", "")),
                )
            )
        return "paired", driven

    if isinstance(script[0], dict):
        driven = []
        for i, item in enumerate(script):
            turn_index = int(item.get("轮次", i + 1))
            driven.append(
                DrivenTurn(
                    turn_index=turn_index,
                    assistant_turn_index=turn_index,
                    goal=str(item.get("脚本目标") or item.get("目标") or ""),
                    condition=str(item.get("条件规则") or item.get("条件") or ""),
                )
            )
        return "same", driven

    # list[str]
    driven = []
    for i, item in enumerate(script):
        turn_index = _extract_turn_num(str(item), fallback=i + 1)
        driven.append(
            DrivenTurn(
                turn_index=turn_index,
                assistant_turn_index=turn_index,
                goal=str(item),
                condition="",
            )
        )
    return "same", driven
