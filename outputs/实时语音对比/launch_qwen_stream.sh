#!/usr/bin/env bash
set -euo pipefail

runtime_root="$HOME/vllm-omni-qwen"
model_path="/mnt/c/Users/mozi/Documents/Codex/2026-05-13/d-openclaw-qq-bot-handoff-openclaw/qwen3-tts/models/Qwen3-TTS-12Hz-1.7B-Base"
stage_overrides='{"0":{"max_num_seqs":1,"kv_cache_memory_bytes":805306368,"max_num_batched_tokens":4096},"1":{"max_num_seqs":1,"kv_cache_memory_bytes":268435456,"max_model_len":4096,"max_num_batched_tokens":4096,"enforce_eager":true}}'
log_path="/mnt/c/Users/mozi/Documents/Codex/2026-09-22/codex-threads-01a0c798-e451-7d22-bd56-4/outputs/实时语音对比/vllm_qwen_stream.log"

cd "$runtime_root"
export CUDA_HOME="$runtime_root/.venv/lib/python3.12/site-packages/nvidia/cu13"
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib:${LD_LIBRARY_PATH:-}"
: > "$log_path"
.venv/bin/vllm serve "$model_path" \
  --omni \
  --port 8091 \
  --host 0.0.0.0 \
  --trust-remote-code \
  --stage-overrides "$stage_overrides" 2>&1 | tee -a "$log_path"
