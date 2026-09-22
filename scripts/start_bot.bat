@echo off
REM Manual restart helper for the crypto-options-bot.
REM Use this after a Task Manager End Task, or whenever the bot is dead and NSSM didn't auto-restart.
REM
REM Usage: run from any shell, or double-click from Explorer.
REM Side effects: kills any existing python.exe for the bot, starts a fresh one.

setlocal

set "PROJ=%~dp0.."
set "PYTHON=C:\Program Files\Python312\python.exe"
set "LOG_OUT=%PROJ%\logs\bot_direct_run.out.log"
set "LOG_ERR=%PROJ%\logs\bot_direct_run.err.log"

echo [%date% %time%] Starting crypto-options-bot...

REM Kill any leftover direct-run python processes
taskkill /F /IM python.exe /FI "WINDOWTITLE eq crypto_options_bot*" 2>nul >nul

REM Make sure logs dir exists
if not exist "%PROJ%\logs" mkdir "%PROJ%\logs"

REM Start fresh python process, hidden window
start "" /B "%PYTHON%" -u -m crypto_options_bot paper --feed ws --dashboard-port 8511 1>"%LOG_OUT%" 2>"%LOG_ERR%"

echo [%date% %time%] Started. Logs: %LOG_ERR%
echo Tail with:  powershell -Command "Get-Content '%LOG_ERR%' -Tail 20 -Wait"
endlocal
