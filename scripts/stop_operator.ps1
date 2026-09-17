# Operator — graceful stop. Reads PID file and sends SIGTERM (via taskkill).

$ErrorActionPreference = 'SilentlyContinue'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$PidFile = Join-Path $ProjectDir "logs\operator.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "No PID file at $PidFile; nothing to stop"
    exit 0
}

$pid = Get-Content $PidFile -ErrorAction SilentlyContinue
if (-not $pid) {
    Write-Host "PID file empty; cleaning up"
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    exit 0
}

$proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host "PID $pid not alive; cleaning up"
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    exit 0
}

Write-Host "Stopping operator PID=$pid"
try {
    Stop-Process -Id $pid -Force -ErrorAction Stop
} catch {
    Write-Host "Stop-Process failed: $_"
}

# Give it a moment, then clean up PID file.
Start-Sleep -Seconds 2
Remove-Item $PidFile -ErrorAction SilentlyContinue
Write-Host "Done."
