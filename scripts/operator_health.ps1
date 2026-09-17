# Operator — health probe. Returns exit 0 if alive and recent, 1 otherwise.
# Designed for Task Scheduler + external monitors.

$ErrorActionPreference = 'SilentlyContinue'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$PidFile = Join-Path $ProjectDir "logs\operator.pid"
$Heartbeat = Join-Path $ProjectDir "data_cache\operator.heartbeat"

# 1. Process alive?
$alive = $false
$opPid = $null
if (Test-Path $PidFile) {
    $opPid = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($opPid -and (Get-Process -Id $opPid -ErrorAction SilentlyContinue)) {
        $alive = $true
    }
}
if (-not $alive) {
    Write-Host "[FAIL] operator process not running"
    exit 1
}

# 2. Heartbeat fresh? (must be < 5 minutes old)
if (-not (Test-Path $Heartbeat)) {
    Write-Host "[WARN] heartbeat missing"
    exit 1
}
$age = (Get-Date) - (Get-Item $Heartbeat).LastWriteTime
if ($age.TotalSeconds -gt 300) {
    Write-Host "[FAIL] heartbeat stale: $($age.TotalSeconds)s old"
    exit 1
}

Write-Host "[OK] operator PID=$opPid, heartbeat=$($age.TotalSeconds)s old"
exit 0
