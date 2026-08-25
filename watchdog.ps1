# crypto-options-bot Watchdog
# Monitors bot (Python process) + dashboard (port 8511).  Restarts via
# start_bot_detached.ps1 if either is dead.  Designed to run as a
# Windows Startup-folder task on user logon.
#
# Mirrors kotak-neo-bot's watchdog.ps1.

$ErrorActionPreference = 'Stop'
$project = 'C:\Users\saini\.minimax-agent\projects\crypto-options-bot'
$logFile = Join-Path $project 'logs\watchdog.log'

function Write-WLog {
    param([string]$msg)
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $logFile -Value ("[" + $ts + "] " + $msg) -Encoding UTF8
}

$logDir = Split-Path $logFile
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

Write-WLog ("=== Watchdog started (PID " + $PID + ") ===")
Write-WLog ("Project: " + $project)
Write-WLog "Will check every 60s, restart bot/dashboard if dead"

$checkIntervalSec     = 60
$restartCooldownSec   = 30
$lastBotRestart       = (Get-Date).AddSeconds(-$restartCooldownSec - 1)
$lastDashRestart      = (Get-Date).AddSeconds(-$restartCooldownSec - 1)
$DashboardPort        = 8511

while ($true) {
    try {
        # ---- bot process check ----
        $botProcs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'crypto_options_bot' })
        $botAlive = ($botProcs.Count -gt 0)

        if (-not $botAlive) {
            $now = Get-Date
            $sinceLast = ($now - $lastBotRestart).TotalSeconds
            if ($sinceLast -ge $restartCooldownSec) {
                Write-WLog "ALERT: bot DEAD - invoking start_bot_detached.ps1"
                try {
                    $out = & "$project\start_bot_detached.ps1" 2>&1
                    Write-WLog ("  start_bot_detached.ps1 invoked (" + @($out).Count + " lines)")
                    $lastBotRestart = $now
                    Start-Sleep -Seconds 30
                } catch {
                    Write-WLog ("  ERROR: " + $_.ToString())
                }
            } else {
                Write-WLog ("Bot dead but within cooldown (" + [int]$sinceLast + "s), skipping")
            }
        }

        # ---- dashboard port check (TCP first, then HTTP /api/status) ----
        $dashAlive = $false
        try {
            $tcpOk = (Test-NetConnection -ComputerName 127.0.0.1 -Port $DashboardPort -InformationLevel Quiet -WarningAction SilentlyContinue)
            if ($tcpOk) {
                try {
                    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$DashboardPort/api/status" -TimeoutSec 4 -UseBasicParsing -ErrorAction SilentlyContinue
                    $dashAlive = ($resp.StatusCode -eq 200)
                } catch {
                    $dashAlive = $true  # TCP up, even if HTTP probe errored
                }
            }
        } catch {
            $dashAlive = $false
        }

        if (-not $dashAlive) {
            $now = Get-Date
            $sinceLast = ($now - $lastDashRestart).TotalSeconds
            if ($sinceLast -ge $restartCooldownSec) {
                Write-WLog "ALERT: dashboard DOWN on :$DashboardPort - invoking start_bot_detached.ps1 (which boots the dashboard too)"
                try {
                    $out = & "$project\start_bot_detached.ps1" 2>&1
                    Write-WLog ("  start_bot_detached.ps1 invoked (" + @($out).Count + " lines)")
                    $lastDashRestart = $now
                    Start-Sleep -Seconds 15
                } catch {
                    Write-WLog ("  ERROR: " + $_.ToString())
                }
            } else {
                Write-WLog ("Dashboard down but within cooldown, skipping")
            }
        }

    } catch {
        Write-WLog ("Outer loop error: " + $_.ToString())
    }

    Start-Sleep -Seconds $checkIntervalSec
}
