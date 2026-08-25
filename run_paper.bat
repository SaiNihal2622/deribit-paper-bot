@echo off
setlocal
set "VENV_DIR=.venv\Scripts"
if not exist "%VENV_DIR%\python.exe" (
  echo Virtual env not found at %VENV_DIR%.
  echo Create one with:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  exit /b 1
)
"%VENV_DIR%\python.exe" -m crypto_options_bot paper %*
endlocal
