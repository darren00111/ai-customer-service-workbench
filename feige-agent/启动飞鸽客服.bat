@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo 桌面运行环境尚未安装。请先在本目录运行：
  echo python -m venv .venv
  echo .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" run.py desktop
