"""Smoke-test YuriOS's LiteLLM provider against the existing Agnes endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import time

from yurios.app.providers.openrouter import LiteLLMChatModel


async def main() -> None:
    model_name = os.environ["AGNES_MODEL"]
    provider = LiteLLMChatModel(
        f"lm_studio/{model_name}",
        os.environ["AGNES_API_KEY"],
        api_base=os.environ["AGNES_BASE_URL"].rstrip("/"),
        thinking=False,
        temperature=0.2,
    )
    started = time.perf_counter()
    first_token = None
    chunks: list[str] = []
    async for chunk in provider.stream(
        [
            {
                "role": "system",
                "content": "你是虚拟角色的心智内核。用一句简短中文回答，不调用工具。",
            },
            {"role": "user", "content": "请确认心智模型通路已接通。"},
        ],
        max_tokens=80,
    ):
        if first_token is None:
            first_token = time.perf_counter()
        chunks.append(chunk)
    finished = time.perf_counter()
    text = "".join(chunks).strip()
    print(
        json.dumps(
            {
                "ok": bool(text),
                "model": model_name,
                "first_token_s": round((first_token or finished) - started, 3),
                "total_s": round(finished - started, 3),
                "chars": len(text),
                "reply": text,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
