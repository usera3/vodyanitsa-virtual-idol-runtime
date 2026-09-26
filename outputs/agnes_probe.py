"""Probe Agnes model availability without ever printing the API key."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://apihub.agnes-ai.com/v1"
TARGET_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
OUTPUT = Path(__file__).with_name("Agnes接口验证.json")


def request_json(path: str, method: str = "GET", payload: dict | None = None) -> tuple[dict, float]:
    key = os.environ.get("AGNES_API_KEY", "")
    if not key:
        raise RuntimeError("AGNES_API_KEY is not configured")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    return result, time.perf_counter() - started


models_payload, models_seconds = request_json("/models")
model_ids = sorted(
    item.get("id", "") for item in models_payload.get("data", []) if isinstance(item, dict) and item.get("id")
)
chat_payload, chat_seconds = request_json(
    "/chat/completions",
    method="POST",
    payload={
        "model": TARGET_MODEL,
        "messages": [{"role": "user", "content": "仅回复 AGNES_OK"}],
        "temperature": 0,
        "max_tokens": 256,
        "stream": False,
    },
)
choice = (chat_payload.get("choices") or [{}])[0]
content = ((choice.get("message") or {}).get("content") or "").strip()
reasoning = ((choice.get("message") or {}).get("reasoning_content") or "").strip()
result = {
    "base_url": BASE_URL,
    "target_model": TARGET_MODEL,
    "target_available": TARGET_MODEL in model_ids,
    "available_models": model_ids,
    "models_seconds": round(models_seconds, 3),
    "chat_seconds": round(chat_seconds, 3),
    "chat_reply": content,
    "reasoning_preview": reasoning[:200],
    "message_fields": sorted((choice.get("message") or {}).keys()),
    "finish_reason": choice.get("finish_reason"),
    "usage": chat_payload.get("usage"),
    "verified": "AGNES_OK" in content,
}
OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
