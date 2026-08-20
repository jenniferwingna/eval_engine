"""Minimal OpenAI-compatible chat completion client.

Reuses the same key-file convention as cockpit_harness (harness/config.py):
a plain-text API key file, one key per file, no env var required.

This module has zero dependency on cockpit_harness code — it only needs
the key file path — so eval_engine stays self-contained and can be zipped
and handed to someone else without dragging the harness repo along.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_KEY_FILE = Path("/Users/yugengyun/harness_rag/cockpit_harness/.qwen.key")
DEFAULT_API_BASE = "https://llm-vu1evj21sldye2c3.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen3-omni-flash"


@dataclass
class ChatResponse:
    text: str
    latency_ms: float
    raw: dict


def _load_key(key_file: Path) -> str:
    try:
        return key_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read API key file {key_file}: {exc}") from exc


class ChatClient:
    def __init__(
        self,
        key_file: Path = DEFAULT_KEY_FILE,
        api_base: str = DEFAULT_API_BASE,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 30.0,
        ssl_verify: bool = False,  # matches cockpit_harness's own DEEPSEEK_SSL_VERIFY=0 default for this network
    ) -> None:
        self.api_key = _load_key(key_file)
        self.api_base = api_base
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._ssl_context = None if ssl_verify else ssl._create_unverified_context()

    def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 400,
    ) -> ChatResponse:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.api_base,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=self._ssl_context) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"chat completion HTTP {exc.code}: {detail[:500]}") from exc
        latency_ms = (time.perf_counter() - start) * 1000
        text = ""
        choices = raw.get("choices") or []
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "")
        return ChatResponse(text=text.strip(), latency_ms=latency_ms, raw=raw)
