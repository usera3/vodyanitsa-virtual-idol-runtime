"""Measure cosine similarity with the same CAM++ speaker encoder used by CosyVoice."""
from pathlib import Path

import numpy as np
import onnxruntime as ort
import soundfile as sf
import torch
import torchaudio
import torchaudio.compliance.kaldi as kaldi

MODEL = Path(r"D:\AI\Models\Fun-CosyVoice3-0.5B-2512\campplus.onnx")
REFERENCE = Path(r"C:\Users\mozi\Documents\Codex\2026-05-13\d-openclaw-qq-bot-handoff-openclaw\openclaw-qq-model-and-voice\qwen3-tts-apple-silicon\voices\akari2.wav")
OUTPUTS = [
    Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比\cosyvoice3_akari2.wav"),
    Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比\qwen3tts_17b_akari2.wav"),
    Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比\qwen3tts_17b_base_cached_akari2.wav"),
    Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比\qwen3tts_06b_base_akari2.wav"),
    Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比\qwen3tts_06b_base_sdpa_akari2.wav"),
    Path(r"C:\Users\mozi\Documents\Codex\2026-09-22\codex-threads-01a0c798-e451-7d22-bd56-4\outputs\实时语音对比\qwen3tts_17b_vllm_stream_akari2.wav"),
]


def load_16k(path: Path) -> torch.Tensor:
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    waveform = torch.from_numpy(audio.T).mean(dim=0, keepdim=True)
    return torchaudio.functional.resample(waveform, rate, 16000) if rate != 16000 else waveform


def embedding(session: ort.InferenceSession, path: Path) -> np.ndarray:
    feat = kaldi.fbank(load_16k(path), num_mel_bins=80, dither=0, sample_frequency=16000)
    feat = feat - feat.mean(dim=0, keepdim=True)
    value = session.run(None, {session.get_inputs()[0].name: feat.unsqueeze(0).numpy()})[0].flatten()
    return value / np.linalg.norm(value)


session = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
reference = embedding(session, REFERENCE)
for output in OUTPUTS:
    score = float(np.dot(reference, embedding(session, output)))
    print(f"{output.name}\t{score:.6f}")
