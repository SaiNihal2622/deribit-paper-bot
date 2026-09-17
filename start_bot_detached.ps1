# Detached launcher for crypto-options-bot (no NSSM, plain background)
# Use this when NSSM is not desired (e.g. testing on a dev box, no admin).
# Logs to logs\bot_stdout.log and logs\bot_stderr.log.
#
# Usage:  .\start_bot_detached.ps1
#         .\start_bot_detached.ps1 -Feed rest
#         .\start_bot_detached.ps1 -Mode live

param(
    [ValidateSet('paper','live')]
    [string]$Mode = 'paper',
    [ValidateSet('ws','rest')]
    [string]$Feed = 'ws',
    [int]$DashboardPort = 8511
)

$ErrorActionPreference = 'Stop'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$LogDir     = Join-Path $ProjectDir "logs"
$VenvPy     = Join-Path $ProjectDir ".venv\Scripts\python.exe"

# Prefer a project-local venv; fall back to whatever `python` is on PATH
# (no admin needed, no install step required for dev boxes).
if (Test-Path $VenvPy) {
    $Py = $VenvPy
} else {
    $Py = (Get-Command python).Source
    Write-Host "  (no .venv found - using system python: $Py)"
}

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

$stdout = Join-Path $LogDir "bot_stdout.log"
$stderr = Join-Path $LogDir "bot_stderr.log"

$args = @('-u','-m','crypto_options_bot', $Mode, '--feed', $Feed, '--dashboard-port', $DashboardPort)

Write-Host "Launching: $Py $($args -join ' ')"
Write-Host "  stdout: $stdout"
Write-Host "  stderr: $stderr"

$proc = Start-Process -FilePath $Py `
                      -ArgumentList $args `
                      -WorkingDirectory $ProjectDir `
                      -RedirectStandardOutput $stdout `
                      -RedirectStandardError $stderr `
                      -NoNewWindow `
                      -PassThru
Write-Host "  PID = $($proc.Id)"
Set-Content -Path (Join-Path $LogDir "bot.pid") -Value $proc.Id
