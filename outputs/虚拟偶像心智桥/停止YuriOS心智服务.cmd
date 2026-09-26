@echo off
chcp 65001 >nul
wsl -d Ubuntu-24.04 -- bash -lc "export YURIOS_ROOT=/home/mozi/yurios-eval/runtime; cd /home/mozi/yurios-eval/runtime; /home/mozi/yurios-eval/.venv/bin/yurios stop"
pause
