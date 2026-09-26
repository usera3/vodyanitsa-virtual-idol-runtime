@echo off
chcp 65001 >nul
"%~dp0.venv\Scripts\python.exe" "%~dp0voice_avatar_bridge.py" --text "你好，这是实时生成的口型同步测试。现在我的嘴型会跟着声音一起变化。" --output-wav "%~dp0实时口型测试.wav" --plan "%~dp0实时口型测试.json"
if errorlevel 1 pause
