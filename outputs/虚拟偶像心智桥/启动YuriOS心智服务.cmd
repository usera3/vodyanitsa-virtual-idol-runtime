@echo off
chcp 65001 >nul
wsl -d Ubuntu-24.04 -- bash -lc "export HF_ENDPOINT=https://hf-mirror.com; export YURIOS_ROOT=/home/mozi/yurios-eval/runtime; cd /home/mozi/yurios-eval/runtime; /home/mozi/yurios-eval/.venv/bin/yurios start"
if errorlevel 1 (
  echo YuriOS 启动失败。
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8768/"
