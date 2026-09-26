@echo off
chcp 65001 >nul
setlocal
set "BLENDER_USER_SCRIPTS=C:\Users\mozi\Documents\Codex\2026-08-16\wo\work\mmd_auto\vendor\blender_user_scripts"
set "BLENDER=C:\Users\mozi\Documents\Codex\2026-08-16\wo\work\mmd_auto\vendor\blender\blender-5.2.0-windows-x64\blender-5.2.0-windows-x64\blender.exe"
start "" "%BLENDER%" --factory-startup --threads 4 --python "%~dp0启动薇斯纳.py" -- --hair-physics
endlocal
