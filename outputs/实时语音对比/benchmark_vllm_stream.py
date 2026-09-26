"""Measure Qwen3-TTS vLLM-Omni PCM time-to-first-audio and total latency."""
import base64
import json
import time
import wave
from pathlib import Path

import requests

REFERENCE = Path(r"C:\Users\mozi\Documents\Codex\2026-05-13\d-openclaw-qq-bot-handoff-openclaw\openclaw-qq-model-and-voice\qwen3-tts-apple-silicon\voices\akari2.wav")
OUTPUT = Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比\qwen3tts_17b_vllm_stream_akari2.wav")
URL = "http://127.0.0.1:8091/v1/audio/speech"
TEXT = "你好，我回来了。刚才让你久等了吗？"

ref_data = "data:audio/wav;base64," + base64.b64encode(REFERENCE.read_bytes()).decode("ascii")
payload = {
    "input": TEXT,
    "task_type": "Base",
    "language": "Chinese",
    "ref_audio": ref_data,
    "x_vector_only_mode": True,
    "stream": True,
    "stream_format": "audio",
    "response_format": "pcm",
    "initial_codec_chunk_frames": 1,
    "max_new_tokens": 256,
}

runs = []
last_audio = b""
for index in range(2):
    started = time.perf_counter()
    response = requests.post(URL, json=payload, stream=True, timeout=(10, 300))
    headers_received = time.perf_counter()
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
    chunks = []
    first_audio_at = None
    for chunk in response.iter_content(chunk_size=None):
        if not chunk:
            continue
        if first_audio_at is None:
            first_audio_at = time.perf_counter()
        chunks.append(chunk)
    finished = time.perf_counter()
    last_audio = b"".join(chunks)
    runs.append(
        {
            "run": index + 1,
            "headers_seconds": round(headers_received - started, 3),
            "first_audio_seconds": round(first_audio_at - started, 3),
            "total_seconds": round(finished - started, 3),
            "chunks": len(chunks),
            "audio_bytes": len(last_audio),
            "audio_seconds": round(len(last_audio) / (24000 * 2), 3),
        }
    )

with wave.open(str(OUTPUT), "wb") as wav:
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(24000)
    wav.writeframes(last_audio)

print(json.dumps({"runs": runs, "output": str(OUTPUT)}, ensure_ascii=False))
