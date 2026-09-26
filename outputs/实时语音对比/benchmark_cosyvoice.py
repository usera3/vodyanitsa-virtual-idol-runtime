"""Benchmark CosyVoice3 streaming with the same akari2 reference used by Qwen3-TTS."""
from __future__ import annotations
import argparse,json,sys,time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch,torchaudio

REPO=Path(r'D:\AI\Runtimes\CosyVoice3')
sys.path.insert(0,str(REPO))
sys.path.insert(0,str(REPO/'third_party'/'Matcha-TTS'))
from cosyvoice.cli.cosyvoice import AutoModel
import cosyvoice.cli.frontend as cosy_frontend

# torchaudio 2.11 delegates file I/O to TorchCodec, whose Windows wheel needs
# separately installed shared FFmpeg DLLs. Keep the model/runtime untouched and
# use soundfile for the benchmark's WAV I/O instead.
def _load_wav_soundfile(path: str, target_rate: int) -> torch.Tensor:
    audio, rate = sf.read(path, dtype='float32', always_2d=True)
    waveform = torch.from_numpy(np.asarray(audio).T).mean(dim=0, keepdim=True)
    if rate != target_rate:
        waveform = torchaudio.functional.resample(waveform, rate, target_rate)
    return waveform

cosy_frontend.load_wav = _load_wav_soundfile

MODEL=Path(r'D:\AI\Models\Fun-CosyVoice3-0.5B-2512')
REFERENCE=Path(r'C:\Users\mozi\Documents\Codex\2026-05-13\d-openclaw-qq-bot-handoff-openclaw\openclaw-qq-model-and-voice\qwen3-tts-apple-silicon\voices\akari2.wav')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--text',default='你好，我回来了。刚才让你久等了吗？')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--stream',action='store_true')
    parser.add_argument('--repeat',type=int,default=2)
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True)
    torch.cuda.reset_peak_memory_stats();started=time.perf_counter()
    model=AutoModel(model_dir=str(MODEL));loaded=time.perf_counter()
    prompt='You are a helpful assistant.<|endofprompt|>'+args.text
    runs=[];audio=None
    for run_index in range(args.repeat):
        chunks=[];first=None;run_started=time.perf_counter()
        for item in model.inference_cross_lingual(prompt,str(REFERENCE),stream=args.stream):
            if first is None:first=time.perf_counter()
            chunks.append(item['tts_speech'].cpu())
        finished=time.perf_counter()
        if not chunks:raise RuntimeError('CosyVoice returned no audio chunks')
        audio=torch.cat(chunks,dim=1)
        audio_seconds=audio.shape[1]/model.sample_rate
        runs.append({'run':run_index+1,'first_chunk_seconds':round(first-run_started,3),
                     'generation_seconds':round(finished-run_started,3),'chunks':len(chunks),
                     'audio_seconds':round(audio_seconds,3),'rtf':round((finished-run_started)/audio_seconds,3)})
    sf.write(str(args.output),audio.squeeze(0).numpy(),model.sample_rate)
    result={'model':'Fun-CosyVoice3-0.5B-2512','stream':args.stream,
            'load_seconds':round(loaded-started,3),'runs':runs,
            'peak_torch_mb':round(torch.cuda.max_memory_allocated()/1048576),'output':str(args.output)}
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
