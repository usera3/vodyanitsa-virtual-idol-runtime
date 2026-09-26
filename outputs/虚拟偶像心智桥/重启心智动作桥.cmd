@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
set "PY=%ROOT%..\虚拟偶像语音桥\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo 找不到 Python: %PY%
  pause
  exit /b 1
)
echo 正在结束旧的心智动作桥...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*mind_action_bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul
echo 正在启动新的拟人待机层...
"%PY%" "%ROOT%mind_action_bridge.py" --config "%ROOT%config.json"
pause
