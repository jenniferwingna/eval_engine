"""Vendored, trimmed copy of cockpit_harness/harness/core/tool_schema.py's
argument-canonicalization tables.

Deliberately duplicated rather than imported: eval_engine is meant to be
zipped and handed to someone who may not have the cockpit_harness repo
alongside it, and it also needs to eventually judge OTHER SUTs (per
HARNESS_SUT_CONTRACT.md), not just this one local harness. Keep this file
in sync by hand if the harness's own alias tables change.
"""
from __future__ import annotations

import re

SWITCH_OPEN = {"on", "open", "enable", "start", "unlock", "打开", "开启", "启用", "启动"}
SWITCH_CLOSE = {"off", "close", "disable", "stop", "lock", "关闭", "关掉", "停用", "禁用"}
FUNCTION_INC = {"inc", "increase", "up", "high", "higher", "raise", "add", "调高", "调大", "增大", "升高"}
FUNCTION_DEC = {"dec", "decrease", "down", "low", "lower", "reduce", "调低", "调小", "减小", "降低"}
POSITION_ALIASES = {
    "main": "driver", "frontLeft": "driver", "主驾": "driver", "驾驶位": "driver",
    "copilot": "copilot", "passenger": "copilot", "frontRight": "copilot", "副驾": "copilot",
    "backLeft": "rear_left", "rearLeft": "rear_left", "secondRowLeft": "rear_left", "后左": "rear_left", "左后": "rear_left",
    "backRight": "rear_right", "rearRight": "rear_right", "secondRowRight": "rear_right", "后右": "rear_right", "右后": "rear_right",
    "secondRow": "rear", "back": "rear", "rear": "rear", "后排": "rear", "后座": "rear",
    "all": "all", "全部": "all", "所有": "all",
}
KEY_ALIASES = {"action": "function", "func": "function", "pos": "position"}


def normalize_key(key: str) -> str:
    return KEY_ALIASES.get(str(key), str(key))


def _normalize_unit(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"%", "percent"} or value in {"百分比", "百分数"}:
        return "percent"
    if lowered in {"level", "gear"} or value in {"档", "挡", "档位", "挡位", "级"}:
        return "level"
    return value


def normalize_value(key: str, value) -> str:
    text = str(value).strip()
    key = normalize_key(key)
    if key == "switch":
        if text in SWITCH_OPEN:
            return "open"
        if text in SWITCH_CLOSE:
            return "close"
        return text
    if key == "function":
        if text.lower() in FUNCTION_INC or text in FUNCTION_INC:
            return "increase"
        if text.lower() in FUNCTION_DEC or text in FUNCTION_DEC:
            return "decrease"
        if text.lower() == "set" or text == "设置":
            return "set"
        return text
    if key == "position":
        return POSITION_ALIASES.get(text, text)
    if key == "unit":
        return _normalize_unit(text)
    return text


def canonicalize_args(args: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_key, value in (args or {}).items():
        key = normalize_key(str(raw_key))
        if key.startswith("_") or value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        out[key] = normalize_value(key, text)
    return out


_KV_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^,)\s]+)")


def parse_kv_string_list(items) -> dict[str, str]:
    """Parse strings like 'function=set' / 'position=backRight' (as used in
    结构化信息断言's list-valued fields) into a flat dict."""
    out: dict[str, str] = {}
    if isinstance(items, str):
        items = [items]
    for item in items or []:
        m = _KV_TOKEN_RE.search(str(item))
        if m:
            out[normalize_key(m.group(1))] = normalize_value(m.group(1), m.group(2))
    return out


_FUNC_CALL_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$")


def parse_func_call_string(text: str) -> tuple[str, dict[str, str]]:
    """Parse 'charging_port_auto_unlock(switch=open)' style strings that show
    up inside 禁止动作/结构化信息断言 free text."""
    m = _FUNC_CALL_RE.match(str(text or ""))
    if not m:
        return "", {}
    name = m.group(1)
    args = parse_kv_string_list(m.group(2).split(","))
    return name, args


def args_match(expected: dict[str, str], actual: dict[str, str]) -> bool:
    """expected is a subset-match against actual (actual may carry extra
    canonicalized keys the case doesn't care about, e.g. unit)."""
    for key, value in expected.items():
        if str(actual.get(key, "")) != str(value):
            return False
    return True
