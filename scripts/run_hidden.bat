@echo off
REM Hidden launcher for scheduled tasks — runs a python script under pythonw.exe with stdout/stderr
REM redirected to the script's named log. No console window flashes.
REM
REM Usage:  run_hidden.bat <script_path> <log_path>
REM
REM %~1 = absolute python script path
REM %~2 = absolute log file path

setlocal
if "%~1"=="" goto :usage
if "%~2"=="" goto :usage

"C:\Program Files\Python312\pythonw.exe" "%~1" > "%~2" 2>&1
exit /b %ERRORLEVEL%

:usage
echo usage: %~nx0 ^<script^> ^<log^>
exit /b 2
