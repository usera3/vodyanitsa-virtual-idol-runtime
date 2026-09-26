"""Benchmark persistent Qwen3-TTS 0.6B Base with a cached clone prompt."""
import json
import os
import time
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

MODEL_NAME = os.environ.get("QWEN_TTS_MODEL", "Qwen3-TTS-12Hz-0.6B-Base")
ATTENTION = os.environ.get("QWEN_TTS_ATTN", "eager")
MODEL = Path(r"C:\Users\mozi\Documents\Codex\2026-05-13\d-openclaw-qq-bot-handoff-openclaw\qwen3-tts\models") / MODEL_NAME
REFERENCE = Path(r"C:\Users\mozi\Documents\Codex\2026-05-13\d-openclaw-qq-bot-handoff-openclaw\openclaw-qq-model-and-voice\qwen3-tts-apple-silicon\voices\akari2.wav")
REFERENCE_TEXT = Path(r"C:\Users\mozi\Documents\Codex\2026-05-13\d-openclaw-qq-bot-handoff-openclaw\openclaw-qq-model-and-voice\qwen3-tts-apple-silicon\voices\akari2.txt")
OUTPUT = Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比") / os.environ.get("QWEN_TTS_OUTPUT", "qwen3tts_06b_base_akari2.wav")
TEXT = "你好，我回来了。刚才让你久等了吗？"
GENERATION = dict(
    do_sample=True,
    max_new_tokens=2048,
    top_k=50,
    top_p=1.0,
    temperature=0.9,
    repetition_penalty=1.05,
    subtalker_dosample=True,
    subtalker_top_k=50,
    subtalker_top_p=1.0,
    subtalker_temperature=0.9,
)

torch.cuda.reset_peak_memory_stats()
started = time.perf_counter()
model = Qwen3TTSModel.from_pretrained(
    str(MODEL), device_map="cuda:0", dtype=torch.bfloat16, attn_implementation=ATTENTION
)
loaded = time.perf_counter()
prompt = model.create_voice_clone_prompt(
    ref_audio=str(REFERENCE),
    ref_text=REFERENCE_TEXT.read_text(encoding="utf-8").strip(),
    x_vector_only_mode=True,
)
prompted = time.perf_counter()
runs = []
last_wav = None
last_sr = None
for index in range(2):
    run_started = time.perf_counter()
    wavs, sr = model.generate_voice_clone(
        text=TEXT, language="Chinese", voice_clone_prompt=prompt, **GENERATION
    )
    run_finished = time.perf_counter()
    last_wav, last_sr = wavs[0], sr
    audio_seconds = len(last_wav) / sr
    runs.append(
        {
            "run": index + 1,
            "generation_seconds": round(run_finished - run_started, 3),
            "audio_seconds": round(audio_seconds, 3),
            "rtf": round((run_finished - run_started) / audio_seconds, 3),
        }
    )

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
sf.write(OUTPUT, last_wav, last_sr)
print(
    json.dumps(
        {
            "model": MODEL_NAME,
            "attention": ATTENTION,
            "load_seconds": round(loaded - started, 3),
            "clone_prompt_seconds": round(prompted - loaded, 3),
            "runs": runs,
            "peak_torch_mb": round(torch.cuda.max_memory_allocated() / 1048576),
            "output": str(OUTPUT),
        },
        ensure_ascii=False,
    )
)
