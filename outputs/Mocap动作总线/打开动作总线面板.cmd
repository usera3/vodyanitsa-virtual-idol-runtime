@echo off
chcp 65001 >nul
start "" "%~dp0Mocap动作总线.exe" --state-dir "%~dp0运行状态"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:39538/"
