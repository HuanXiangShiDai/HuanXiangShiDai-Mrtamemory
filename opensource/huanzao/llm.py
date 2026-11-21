"""DeepSeek 最小客户端：只读凭据，不写文件，不把 key 打到日志里。"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
_CRED = Path(os.path.expanduser("~")) / ".dsh" / ".credentials.yaml"


def load_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    if _CRED.exists():
        for line in _CRED.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "DEEPSEEK_API_KEY" in line:
                m = re.search(r"(sk-[A-Za-z0-9\-_]+)", line)
                if m:
                    return m.group(1)
    return ""


def has_key() -> bool:
    return bool(load_key())


def chat(messages: list[dict], *, model: str = DEFAULT_MODEL, temperature: float = 0.0,
         max_tokens: int = 900, timeout: int = 90, want_usage: bool = False,
         json_mode: bool = True):
    key = load_key()
    if not key:
        raise RuntimeError("没有可用的 DEEPSEEK_API_KEY（环境变量与 ~/.dsh/.credentials.yaml 都没有）")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    text = data["choices"][0]["message"]["content"]
    if want_usage:
        return text, data.get("usage", {})
    return text
