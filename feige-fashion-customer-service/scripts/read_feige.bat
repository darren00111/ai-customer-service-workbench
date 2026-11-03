@echo off
REM Read the currently open Feige conversation (READ-ONLY).
REM Requires: start_feige_browser.bat was run first (CDP on port 9222).

setlocal
set PORT=9222
set DIR=%~dp0
set PY=C:\Users\1154\.workbuddy\binaries\python\versions\3.13.12\python.exe
set AB=C:\Users\1154\.workbuddy\binaries\node\versions\22.22.2-3\node_modules\agent-browser\bin\agent-browser-win32-x64.exe
set T=%TEMP%\feige_payload.txt

"%PY%" "%DIR%read_conversation.py" --emit-payload "%T%" || goto :fail

set /p PAYLOAD=<"%T%"
"%AB%" --cdp %PORT% --max-output 200000 eval "%PAYLOAD%" > "%TEMP%\feige_raw.json"

"%PY%" "%DIR%read_conversation.py" --from-raw "%TEMP%\feige_raw.json"
goto :end

:fail
echo Failed to build payload.

:end
endlocal
pause
