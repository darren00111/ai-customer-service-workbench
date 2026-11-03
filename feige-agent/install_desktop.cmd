@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_desktop.ps1" -Launch
if errorlevel 1 pause
