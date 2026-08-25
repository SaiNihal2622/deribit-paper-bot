# Plain-shell supervisor for crypto-options-bot (no NSSM).
# Loops `python -m crypto_options_bot supervisor paper` and reports
# restarts.  Use this when you want a foreground supervisor that
# doesn't require admin.  For a true background service, prefer
# start_bot_service.ps1 (NSSM).
#
# Mirrors kotak-neo-bot's supervisor_loop.ps1.

$ErrorActionPreference = 'Continue'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$LogDir     = Join-Path $ProjectDir "logs"
$VenvPy     = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

$loopLog = Join-Path $LogDir "supervisor_loop.log"
$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $loopLog -Value "[$ts] supervisor_loop.ps1 started (PID $PID)" -Encoding UTF8

while ($true) {
    Set-Location $ProjectDir
    Write-Host "[$ts] launching crypto_options_bot supervisor paper..."
    & $VenvPy -u -m crypto_options_bot supervisor paper 2>&1 | Tee-Object -FilePath (Join-Path $LogDir "supervisor_loop_run.log")
    $rc = $LASTEXITCODE
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $loopLog -Value "[$ts] child exited rc=$rc, restarting in 5s" -Encoding UTF8
    Start-Sleep -Seconds 5
}
