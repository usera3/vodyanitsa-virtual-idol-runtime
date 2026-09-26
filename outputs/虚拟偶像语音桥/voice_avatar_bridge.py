"""Synthesize/play Qwen3-TTS speech and publish synchronized MMD vowel visemes."""
from __future__ import annotations

import argparse
import base64
import json
import math
import socket
import struct
import time
import wave
from pathlib import Path

import numpy as np
import requests
from pypinyin import Style, lazy_pinyin

SAMPLE_RATE = 24000
FPS = 30
VISEMES = ("A", "I", "U", "E", "O")
DEFAULT_REFERENCE = Path(
    r"C:\Users\mozi\Documents\Codex\2026-05-13\d-openclaw-qq-bot-handoff-openclaw\openclaw-qq-model-and-voice\qwen3-tts-apple-silicon\voices\akari2.wav"
)


def osc_string(value: str) -> bytes:
    data = value.encode("utf-8") + b"\0"
    return data + b"\0" * (-len(data) % 4)


def osc_message(address: str, *args: object) -> bytes:
    tags = ","
    body = b""
    for arg in args:
        if isinstance(arg, str):
            tags += "s"
            body += osc_string(arg)
        elif isinstance(arg, int):
            tags += "i"
            body += struct.pack(">i", arg)
        else:
            tags += "f"
            body += struct.pack(">f", float(arg))
    return osc_string(address) + osc_string(tags) + body


def osc_bundle(messages: list[bytes]) -> bytes:
    return b"#bundle\0" + struct.pack(">Q", 1) + b"".join(struct.pack(">I", len(item)) + item for item in messages)


def publish_visemes(sock: socket.socket, port: int, values: np.ndarray) -> None:
    messages = [osc_message("/VMC/Ext/Blend/Val", name, float(value)) for name, value in zip(VISEMES, values)]
    messages.extend((osc_message("/VMC/Ext/Blend/Apply"), osc_message("/VMC/Ext/OK", 1)))
    sock.sendto(osc_bundle(messages), ("127.0.0.1", port))


def synthesize(text: str, ref_audio: Path, url: str) -> np.ndarray:
    ref_data = "data:audio/wav;base64," + base64.b64encode(ref_audio.read_bytes()).decode("ascii")
    payload = {
        "input": text,
        "task_type": "Base",
        "language": "Chinese",
        "ref_audio": ref_data,
        "x_vector_only_mode": True,
        "stream": True,
        "stream_format": "audio",
        "response_format": "pcm",
        "initial_codec_chunk_frames": 1,
        "max_new_tokens": max(128, min(1024, len(text) * 10)),
    }
    response = requests.post(url, json=payload, stream=True, timeout=(10, 300))
    response.raise_for_status()
    pcm = b"".join(chunk for chunk in response.iter_content(chunk_size=None) if chunk)
    if not pcm:
        raise RuntimeError("TTS returned no PCM audio")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != SAMPLE_RATE:
            raise ValueError("WAV must be mono 16-bit PCM at 24000 Hz")
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def final_to_visemes(pinyin: str) -> tuple[str, ...]:
    value = pinyin.lower().replace("ü", "v")
    for initial in ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w"):
        if value.startswith(initial):
            value = value[len(initial) :]
            break
    special = {
        "ai": ("A", "I"), "ei": ("E", "I"), "ao": ("A", "O"), "ou": ("O", "U"),
        "ia": ("I", "A"), "ie": ("I", "E"), "iao": ("I", "A", "O"), "iu": ("I", "U"),
        "ua": ("U", "A"), "uo": ("U", "O"), "uai": ("U", "A", "I"), "ui": ("U", "I"),
        "ve": ("U", "E"), "ue": ("U", "E"),
    }
    for ending, sequence in sorted(special.items(), key=lambda item: -len(item[0])):
        if value.startswith(ending) or value.endswith(ending):
            return sequence
    if "a" in value:
        return ("A",)
    if "o" in value:
        return ("O",)
    if "e" in value:
        return ("E",)
    if "i" in value:
        return ("I",)
    if "u" in value or "v" in value:
        return ("U",)
    return ("A",)


def text_units(text: str) -> list[tuple[tuple[str, ...] | None, float, str]]:
    pauses = {",": 0.35, "，": 0.35, ";": 0.45, "；": 0.45, ":": 0.3, "：": 0.3,
              ".": 0.65, "。": 0.65, "!": 0.65, "！": 0.65, "?": 0.7, "？": 0.7,
              "、": 0.2, "…": 0.55, " ": 0.15, "\n": 0.65}
    units: list[tuple[tuple[str, ...] | None, float, str]] = []
    for char in text:
        if char in pauses:
            units.append((None, pauses[char], char))
            continue
        if char.isspace():
            continue
        values = lazy_pinyin(char, style=Style.NORMAL, strict=False, errors=lambda item: list(item))
        pinyin = values[0] if values else char
        units.append((final_to_visemes(pinyin), 1.0, char))
    return units or [(("A",), 1.0, "")]


def rms_envelope(audio: np.ndarray) -> np.ndarray:
    count = max(1, math.ceil(len(audio) / (SAMPLE_RATE / FPS)))
    values = np.zeros(count, dtype=np.float32)
    half = int(SAMPLE_RATE * 0.02)
    for index in range(count):
        center = int(index * SAMPLE_RATE / FPS)
        chunk = audio[max(0, center - half) : min(len(audio), center + half)]
        values[index] = math.sqrt(float(np.mean(chunk * chunk)) + 1e-10) if len(chunk) else 0.0
    if len(values) > 2:
        values = np.convolve(values, np.array([0.2, 0.6, 0.2], dtype=np.float32), mode="same")
    floor = float(np.percentile(values, 12))
    ceiling = float(np.percentile(values, 92))
    return np.clip((values - floor) / max(1e-5, ceiling - floor), 0.0, 1.0)


def build_plan(text: str, audio: np.ndarray) -> tuple[np.ndarray, list[dict[str, object]]]:
    units = text_units(text)
    envelope = rms_envelope(audio)
    duration = len(audio) / SAMPLE_RATE
    weights = np.array([item[1] for item in units], dtype=np.float64)
    boundaries = np.concatenate(([0.0], np.cumsum(weights) / weights.sum() * duration))

    # Pull predicted inter-syllable boundaries toward nearby acoustic valleys.
    search = max(1, round(0.08 * FPS))
    for index in range(1, len(boundaries) - 1):
        center = int(boundaries[index] * FPS)
        lo, hi = max(1, center - search), min(len(envelope) - 1, center + search + 1)
        if hi > lo:
            boundaries[index] = (lo + int(np.argmin(envelope[lo:hi]))) / FPS
    boundaries = np.maximum.accumulate(boundaries)
    boundaries[-1] = duration

    plan = np.zeros((len(envelope), len(VISEMES)), dtype=np.float32)
    unit_debug = []
    for unit_index, (sequence, _, char) in enumerate(units):
        start, end = boundaries[unit_index], boundaries[unit_index + 1]
        unit_debug.append({"text": char, "visemes": list(sequence or ()), "start": round(start, 3), "end": round(end, 3)})
        if not sequence or end <= start:
            continue
        consonant = min(0.075, (end - start) * 0.2)
        for frame in range(max(0, int(start * FPS)), min(len(plan), math.ceil(end * FPS))):
            t = (frame / FPS - start) / max(1e-5, end - start)
            if frame / FPS < start + consonant:
                openness = max(0.0, t / 0.2) * 0.35
            else:
                openness = 1.0
            openness *= float(envelope[frame]) ** 0.65
            if openness < 0.035:
                continue
            position = max(0.0, min(0.999, (t - 0.18) / 0.82)) * len(sequence)
            first = min(len(sequence) - 1, int(position))
            blend = position - first
            plan[frame, VISEMES.index(sequence[first])] += openness * (1.0 - blend)
            if first + 1 < len(sequence):
                plan[frame, VISEMES.index(sequence[first + 1])] += openness * blend
    # Low-pass visemes to avoid hard shape-key snapping while preserving timing.
    smoothed = np.zeros_like(plan)
    state = np.zeros(len(VISEMES), dtype=np.float32)
    for index, target in enumerate(plan):
        attack = 0.72 if target.max() > state.max() else 0.52
        state += (target - state) * attack
        total = float(state.sum())
        if total > 1.0:
            state /= total
        smoothed[index] = state
    return smoothed, unit_debug


def play_and_publish(audio: np.ndarray, plan: np.ndarray, port: int, latency_offset_ms: float) -> None:
    import sounddevice as sd

    cursor = 0
    anchor_sample = 0
    anchor_dac = 0.0

    def callback(outdata, frames, time_info, status):
        nonlocal cursor, anchor_sample, anchor_dac
        if status:
            print(f"audio status: {status}")
        start = cursor
        end = min(len(audio), start + frames)
        outdata.fill(0)
        if end > start:
            outdata[: end - start, 0] = audio[start:end]
        anchor_sample = start
        anchor_dac = float(time_info.outputBufferDacTime)
        cursor = end
        if cursor >= len(audio):
            raise sd.CallbackStop

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    closed = np.zeros(len(VISEMES), dtype=np.float32)
    publish_visemes(sock, port, closed)
    try:
        with sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=480, callback=callback) as stream:
            while stream.active:
                audible_sample = anchor_sample + max(0.0, float(stream.time) - anchor_dac) * SAMPLE_RATE
                audible_sample += latency_offset_ms / 1000.0 * SAMPLE_RATE
                frame = min(len(plan) - 1, max(0, int(audible_sample / SAMPLE_RATE * FPS)))
                publish_visemes(sock, port, plan[frame])
                time.sleep(1 / FPS)
    finally:
        for _ in range(4):
            publish_visemes(sock, port, closed)
            time.sleep(1 / FPS)
        sock.close()


def publish_ready(path: Path, audio_seconds: float) -> None:
    """Atomically signal that synthesis/planning finished and playback is next."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": "audio_ready",
        "audio_seconds": round(audio_seconds, 3),
        "monotonic_ms": round(time.perf_counter() * 1000.0, 3),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--output-wav", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--ref-audio", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--tts-url", default="http://127.0.0.1:8091/v1/audio/speech")
    parser.add_argument("--voice-port", type=int, default=39543)
    parser.add_argument("--latency-offset-ms", type=float, default=0.0)
    parser.add_argument("--no-playback", action="store_true")
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()

    started = time.perf_counter()
    audio = read_wav(args.wav) if args.wav else synthesize(args.text, args.ref_audio, args.tts_url)
    synthesized = time.perf_counter()
    if args.output_wav:
        write_wav(args.output_wav, audio)
    plan, units = build_plan(args.text, audio)
    planned = time.perf_counter()
    if args.plan:
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        args.plan.write_text(json.dumps({"fps": FPS, "visemes": VISEMES, "units": units, "frames": plan.round(4).tolist()}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.ready_file:
        publish_ready(args.ready_file, len(audio) / SAMPLE_RATE)
    if not args.no_playback:
        play_and_publish(audio, plan, args.voice_port, args.latency_offset_ms)
    print(json.dumps({
        "audio_seconds": round(len(audio) / SAMPLE_RATE, 3),
        "synthesis_seconds": round(synthesized - started, 3),
        "planning_seconds": round(planned - synthesized, 3),
        "viseme_frames": len(plan),
        "voice_port": args.voice_port,
        "played": not args.no_playback,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
