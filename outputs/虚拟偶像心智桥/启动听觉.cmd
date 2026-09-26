@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
set "PY=%ROOT%..\虚拟偶像语音桥\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo 找不到 Python: %PY%
  pause
  exit /b 1
)
echo 使用本机 faster-whisper-base，文字送给 YuriOS。
"%PY%" "%ROOT%hearing_bridge.py" --config "%ROOT%config.json"
pause
