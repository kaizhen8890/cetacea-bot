@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 请先双击 configure.cmd 完成本机配置。
  pause
  exit /b 1
)
.venv\Scripts\python.exe -X utf8 bot.py
pause
