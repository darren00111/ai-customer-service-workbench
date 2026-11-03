@echo off
REM Start a VISIBLE Chrome with CDP for Feige (Douyin shop IM).
REM
REM Why not "agent-browser open":
REM   - it defaults to headless, and --headed does not take effect on this machine
REM   - the headed open and later commands connect to DIFFERENT daemons,
REM     so eval ends up hitting about:blank
REM So we launch Chrome ourselves with a debugging port and let
REM agent-browser attach via --cdp 9222.
REM
REM Usage: double-click this file, or run from cmd.
REM Then:  agent-browser --cdp 9222 eval "<js>"

set PORT=9222
set PROFILE=%TEMP%\wb-chrome-profile
set CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe
set URL=https://im.jinritemai.com/pc_seller_v2/main/workspace

if not exist "%CHROME%" set CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe

echo Chrome : %CHROME%
echo Profile: %PROFILE%
echo Port   : %PORT%
echo.

start "" "%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check --disable-blink-features=AutomationControlled "%URL%"

echo Browser launched. If this is a fresh profile, scan the QR code to log in.
echo Then run:  scripts\read_feige.bat
pause
