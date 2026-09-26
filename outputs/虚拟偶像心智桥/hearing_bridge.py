"""Windows mic → local faster-whisper → YuriOS /api/chat.

Uses the already-downloaded Systran faster-whisper-base on this machine.
YuriOS keeps STT_BACKEND=fake; this process is the ears.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from mind_action_bridge import is_presence_filler

DEFAULT_MODEL = Path(
    r"C:\Users\mozi\.cache\huggingface\hub\models--Systran--faster-whisper-base"
    r"\snapshots\ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
)
SAMPLE_RATE = 16000
FRAME = 512
SPEECH_RMS = 0.002
ONSET_FRAMES = 5
HANGOVER_MS = 1200
MAX_SECONDS = 12.0
MIN_SECONDS = 0.7


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def wsl_yurios_urls(configured: str) -> list[str]:
    urls = [configured]
    try:
        raw = subprocess.check_output(
            ["wsl", "-d", "Ubuntu-24.04", "--", "hostname", "-I"],
            text=True,
            timeout=5,
        )
        ip = (raw.split() or [""])[0].strip()
    except Exception:
        ip = ""
    if ip and "127.0.0.1" in configured:
        urls.append(configured.replace("127.0.0.1", ip))
    return list(dict.fromkeys(urls))


def post_json(url: str, payload: dict, timeout: float = 8.0) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def notify_listen(port: int) -> None:
    try:
        status, _ = post_json(f"http://127.0.0.1:{port}/hearing", {"event": "start"}, timeout=1.0)
        print(f"listen notify {status}", flush=True)
    except Exception as exc:
        print(f"listen notify failed: {exc}", flush=True)


def _win_to_wsl(path: Path) -> str:
    text = str(path.resolve())
    if len(text) >= 2 and text[1] == ":":
        return "/mnt/" + text[0].lower() + text[2:].replace("\\", "/")
    return text.replace("\\", "/")


def send_chat(urls: list[str], text: str, listen_port: int) -> None:
    body = {"text": text, "channel": "hearing", "session_id": "hearing-windows"}
    last_error = ""
    for url in urls:
        try:
            status, reply = post_json(url, body, timeout=20.0)
        except Exception as exc:
            last_error = str(exc)
            continue
        if status < 300:
            print(f"sent [{status}] {text}", flush=True)
            _forward_reply(listen_port, reply)
            return
        last_error = f"{url} -> {status} {reply[:200]}"
    payload_path = Path(os.environ.get("TEMP") or ".") / "yurios-hearing.json"
    payload_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    wsl_file = _win_to_wsl(payload_path)
    try:
        completed = subprocess.run(
            ["wsl", "-d", "Ubuntu-24.04", "--", "curl", "-sS", "-X", "POST",
             "http://127.0.0.1:8768/api/chat",
             "-H", "Content-Type: application/json",
             "--data-binary", f"@{wsl_file}"],
            capture_output=True, timeout=30,
        )
    except Exception as exc:
        print(f"chat failed: {last_error}; wsl {exc}", flush=True)
        return
    stderr = (completed.stderr or b"").decode("utf-8", errors="replace")
    stdout = (completed.stdout or b"").decode("utf-8", errors="replace")
    if completed.returncode == 0:
        print(f"sent [wsl] {text}", flush=True)
        _forward_reply(listen_port, stdout)
        return
    print(f"chat failed: {last_error}; wsl {stderr[:200]}", flush=True)


def _forward_reply(listen_port: int, raw: str) -> None:
    try:
        payload = json.loads(raw)
        reply = str(((payload.get("message") or {}).get("text") or "")).strip()
    except Exception:
        reply = ""
    if not reply:
        print("no assistant text in chat response", flush=True)
        return
    if is_presence_filler(reply):
        print(f"skip presence filler: {reply[:80]}", flush=True)
        return
    print(f"reply: {reply[:80]}", flush=True)
    try:
        post_json(f"http://127.0.0.1:{listen_port}/hearing", {"event": "reply", "text": reply}, timeout=8.0)
    except Exception as exc:
        print(f"reply notify failed: {exc}", flush=True)


def is_meaningful(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def mouth_busy(base: Path, until: float = 0.0) -> bool:
    if time.monotonic() < until:
        return True
    for name in ("拟人状态.json", "运行状态.json"):
        path = base / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("state") or "") in {
            "speaking", "speech_synthesizing", "speech_motion_sync_ready",
        }:
            return True
    return False


def _resample(frame: np.ndarray, src_rate: int) -> np.ndarray:
    if src_rate == SAMPLE_RATE or frame.size == 0:
        return frame.astype(np.float32, copy=False)
    out_len = max(1, int(round(frame.size * SAMPLE_RATE / src_rate)))
    source = np.linspace(0.0, 1.0, frame.size, endpoint=False)
    target = np.linspace(0.0, 1.0, out_len, endpoint=False)
    return np.interp(target, source, frame.astype(np.float32)).astype(np.float32)


def transcribe(model: WhisperModel, audio: np.ndarray) -> str:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 1e-6:
        audio = (audio / peak * 0.8).astype(np.float32)
    segments, _info = model.transcribe(
        audio, language="zh", beam_size=1, vad_filter=False,
    )
    return "".join(segment.text for segment in segments).strip()


def pick_input_device(prefer: str) -> int | None:
    devices = sd.query_devices()
    default = sd.default.device[0]
    if prefer:
        needle = prefer.casefold()
        for index, device in enumerate(devices):
            if device["max_input_channels"] > 0 and needle in str(device["name"]).casefold():
                return index
    for index, device in enumerate(devices):
        name = str(device["name"])
        if device["max_input_channels"] > 0 and "Realtek HD Audio Mic" in name:
            return index
    return default if default is not None and default >= 0 else None


def run(config: dict) -> None:
    hearing = config.get("hearing") or {}
    model_path = Path(str(hearing.get("model_path") or DEFAULT_MODEL))
    if not (model_path / "model.bin").is_file():
        raise SystemExit(f"missing local Whisper model: {model_path}")
    listen_port = int(hearing.get("listen_port") or 39545)
    chat_url = str(hearing.get("yurios_chat_url") or "http://127.0.0.1:8768/api/chat")
    chat_urls = wsl_yurios_urls(chat_url)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    device_index = pick_input_device(str(hearing.get("input_device") or "小度"))
    device_name = sd.query_devices(device_index)["name"] if device_index is not None else "default"
    print(f"loading {model_path}", flush=True)
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", local_files_only=True)
    print(f"ears ready — mic={device_index} {device_name} threshold={SPEECH_RMS}", flush=True)

    speaking = False
    speech_run = 0
    silence_ms = 0.0
    chunks: list[np.ndarray] = []
    preroll: list[np.ndarray] = []
    last_text = ""
    last_sent = 0.0
    last_rms_log = 0.0
    peak_rms = 0.0
    mouth_hold_until = 0.0
    last_busy_check = 0.0
    cached_busy = False
    status_dir = Path(__file__).resolve().parent
    utterances: queue.Queue[np.ndarray] = queue.Queue()

    def currently_busy() -> bool:
        nonlocal last_busy_check, cached_busy, mouth_hold_until
        now = time.monotonic()
        if now - last_busy_check >= 0.15:
            last_busy_check = now
            cached_busy = mouth_busy(status_dir, 0.0)
            if cached_busy:
                mouth_hold_until = now + 1.6
        return cached_busy or now < mouth_hold_until

    def on_frame(frame: np.ndarray) -> None:
        nonlocal speaking, speech_run, silence_ms, last_rms_log, peak_rms
        if currently_busy():
            speaking = False
            speech_run = 0
            chunks.clear()
            preroll.clear()
            return
        rms = float(np.sqrt(np.mean(frame * frame)))
        peak_rms = max(peak_rms, rms)
        is_speech = rms >= SPEECH_RMS
        copy = frame.copy()
        if not speaking:
            preroll.append(copy)
            if len(preroll) > 16:
                preroll.pop(0)
            speech_run = speech_run + 1 if is_speech else 0
            if speech_run >= ONSET_FRAMES:
                speaking = True
                silence_ms = 0.0
                speech_run = 0
                chunks.clear()
                chunks.extend(preroll)
                preroll.clear()
                notify_listen(listen_port)
                print("listening", flush=True)
            return
        chunks.append(copy)
        duration = sum(len(item) for item in chunks) / SAMPLE_RATE
        silence_ms = 0.0 if is_speech else silence_ms + len(copy) / SAMPLE_RATE * 1000.0
        if silence_ms < HANGOVER_MS and duration < MAX_SECONDS:
            return
        speaking = False
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        chunks.clear()
        if duration >= MIN_SECONDS:
            utterances.put(audio)

    def worker() -> None:
        nonlocal last_text, last_sent, mouth_hold_until
        while True:
            audio = utterances.get()
            if currently_busy():
                print("dropped: mouth busy", flush=True)
                continue
            text = transcribe(model, audio)
            if not is_meaningful(text):
                print(f"dropped: {text!r}", flush=True)
                continue
            now = time.monotonic()
            if text == last_text and now - last_sent < 2.0:
                continue
            last_text = text
            last_sent = now
            print(f"heard: {text}", flush=True)
            try:
                post_json(f"http://127.0.0.1:{listen_port}/hearing", {"event": "transcript", "text": text}, timeout=8.0)
            except Exception as exc:
                print(f"transcript notify failed: {exc}", flush=True)
            try:
                send_chat(chat_urls, text, listen_port)
            except Exception as exc:
                print(f"send_chat crashed: {exc}", flush=True)

    threading.Thread(target=worker, name="whisper-worker", daemon=True).start()
    stream = None
    last_error = None
    src_rate = SAMPLE_RATE
    candidates = []
    prefer = str(hearing.get("input_device") or "小度")
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] <= 0:
            continue
        host = sd.query_hostapis(device["hostapi"])["name"]
        name = str(device["name"])
        if prefer not in name:
            continue
        rate = int(device["default_samplerate"] or 44100)
        if host == "Windows WASAPI":
            candidates.append((index, rate))
        elif host == "MME":
            candidates.append((index, rate))
        elif host == "Windows DirectSound":
            candidates.append((index, rate))
    if device_index is not None:
        native = int(sd.query_devices(device_index)["default_samplerate"] or 44100)
        candidates.append((device_index, native))
    candidates.append((None, 44100))
    seen: set[tuple] = set()
    for candidate, rate in candidates:
        key = (candidate, rate)
        if key in seen:
            continue
        seen.add(key)
        try:
            def make_callback(src: int):
                def callback(indata, frames, time_info, status) -> None:
                    on_frame(_resample(indata[:, 0], src))
                return callback

            stream = sd.InputStream(
                device=candidate, samplerate=rate, channels=1, dtype="float32",
                blocksize=FRAME, callback=make_callback(rate),
            )
            stream.start()
            src_rate = rate
            opened = sd.query_devices(candidate)["name"] if candidate is not None else "default"
            print(f"mic open {opened} @{rate}Hz", flush=True)
            break
        except Exception as exc:
            last_error = exc
            print(f"mic skip device={candidate} rate={rate}: {exc}", flush=True)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            stream = None
    if stream is None:
        raise SystemExit(f"cannot open microphone: {last_error}")
    try:
        while True:
            time.sleep(2.0)
            print(f"mic rms={peak_rms:.4f}", flush=True)
            peak_rms = 0.0
    finally:
        stream.stop()
        stream.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    run(load_config(args.config))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
