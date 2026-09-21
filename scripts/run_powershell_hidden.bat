@echo off
REM Hidden powershell launcher for scheduled tasks — runs a powershell script in a
REM minimized window so it doesn't flash on the user's desktop.
REM
REM Usage:  run_powershell_hidden.bat <ps1_script> [args...]
REM
REM Stdout/stderr from powershell are redirected to a sibling .out.log file
REM in the same directory as the .ps1 (so logs/heartbeat.out.log etc.).

setlocal
if "%~1"=="" goto :usage

set "PS1=%~1"
shift

set "LOG=%~dpn1.out.log"
if not "%~1"=="" goto :run

REM No extra args: just run the script under -File
start /min "" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" > "%LOG%" 2>&1
exit /b %ERRORLEVEL%

:run
REM Extra args after the script path — pass them through
start /min "" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %* > "%LOG%" 2>&1
exit /b %ERRORLEVEL%

:usage
echo usage: %~nx0 ^<ps1_script^> [args...]
exit /b 2
