@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto fail
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -X utf8 setup_gui.py
if errorlevel 1 goto fail
exit /b
:fail
echo 配置工具启动失败，请检查 Python 和网络。
pause
