"""LLM user-simulator.

Design rule (see MULTITURN_EVAL_BUILD_PLAN.md Step 1): the simulator is given
a GOAL + CONDITION per turn, not a fixed line, and it only sees the agent's
past `assistant_reply` text (never tool_calls/state) — a real user wouldn't
perceive internal API calls, and exposing them would make the simulator
generate unrealistic utterances that reference implementation details.

This keeps the eval honest: the simulator reacts to what the agent actually
said, so a bad agent turn (e.g. asking the wrong clarifying question) doesn't
get silently steamrolled by a scripted next line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from llm_client import ChatClient

SYSTEM_PROMPT = (
    "你在扮演一位智能座舱语音助手的真实用户。你会看到你的人设、当前场景、"
    "这一轮你想达成的目标，以及一个必须遵守的条件。请只输出这一轮你会对着车说的口语化中文，"
    "不要输出任何解释、标点符号之外的元信息、引号或前缀。\n"
    "硬性要求：\n"
    "1. 只根据目标和条件生成台词，不要提前透露条件要求你隐藏的信息。\n"
    "2. 参考对话历史里 agent 实际说过的话来回应，不要无视 agent 已经做的事情。\n"
    "3. 保持人设里的表达风格（例如口语省略、方言、语气词）。\n"
    "4. 一句话到两句话，不要长篇大论，像真实车内对话。"
)


@dataclass
class SimTurnResult:
    text: str
    attempts: int
    warnings: list[str] = field(default_factory=list)


def _condition_violation_hints(text: str, condition: str) -> list[str]:
    """Cheap heuristic self-check: catch the most common leak — the turn's
    condition explicitly forbids naming a slot that the case wants missing,
    but the generated utterance names it anyway."""
    warnings: list[str] = []
    if "不提供左右" in condition or "不指定" in condition or "不提供" in condition:
        leak_words = ["左", "右", "前排", "后排", "主驾", "副驾", "backLeft", "backRight"]
        hits = [w for w in leak_words if w in text]
        if hits and ("position" in condition or "位" in condition or "排" in condition):
            warnings.append(f"utterance may leak forbidden slot words: {hits}")
    return warnings


def build_user_turn(
    case: dict,
    turn_spec: dict,
    history: list[dict[str, str]],
    variant_note: str = "",
    client: ChatClient | None = None,
    max_attempts: int = 2,
) -> SimTurnResult:
    client = client or ChatClient()
    persona = case.get("用户画像", "")
    sim_config = case.get("模拟用户配置", {})
    scene = case.get("场景背景", "")
    goal = turn_spec.get("目标", "")
    condition = turn_spec.get("条件", "")

    history_lines = []
    for turn in history:
        role = "用户" if turn.get("role") == "user" else "agent"
        history_lines.append(f"{role}: {turn.get('text', '')}")
    history_block = "\n".join(history_lines) if history_lines else "（这是第一轮，还没有历史）"

    user_prompt = (
        f"【你的人设】{persona}\n"
        f"【表达风格】{sim_config.get('表达风格', '')}\n"
        f"【可补充信息，仅在条件允许你补充时才使用】{sim_config.get('可补充信息', {})}\n"
        f"【场景背景】{scene}\n"
        f"【对话历史（只含双方说出口的话，不含内部工具调用）】\n{history_block}\n"
        f"【这一轮你的目标】{goal}\n"
        f"【这一轮你必须遵守的条件】{condition}\n"
        f"{('【本次重跑的变体要求】' + variant_note) if variant_note else ''}\n"
        "请只输出这一轮的台词。"
    )

    warnings: list[str] = []
    text = ""
    for attempt in range(1, max_attempts + 1):
        response = client.complete(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.6 if attempt == 1 else 0.2,
        )
        text = response.text.strip().strip('"').strip("“”")
        hints = _condition_violation_hints(text, condition)
        if not hints:
            return SimTurnResult(text=text, attempts=attempt, warnings=warnings)
        warnings.extend(hints)
        user_prompt += (
            f"\n\n【上一次生成违反了条件，请重新生成】上次输出：{text}\n"
            f"问题：{hints}\n请严格遵守条件，不要包含被禁止透露的信息。"
        )
    return SimTurnResult(text=text, attempts=max_attempts, warnings=warnings)
