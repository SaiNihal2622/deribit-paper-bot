# Operator (6-agent self-evolving system) — detached launcher.
# Usage:  .\start_operator.ps1
#
# Logs: stdout/stderr redirected to logs\operator_stdout.log and
# logs\operator_stderr.log. PID file: logs\operator.pid.

$ErrorActionPreference = 'Stop'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$LogDir = Join-Path $ProjectDir "logs"
$Py = (Get-Command python).Source
$Stdout = Join-Path $LogDir "operator_stdout.log"
$Stderr = Join-Path $LogDir "operator_stderr.log"
$PidFile = Join-Path $LogDir "operator.pid"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }

# Stop existing instance if its PID is alive.
if (Test-Path $PidFile) {
    $old = Get-Content $PidFile -ErrorAction SilentlyContinue
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Host "Stopping existing operator PID=$old"
        Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

Set-Location $ProjectDir
$env:PYTHONPATH = $ProjectDir

$argsList = @('-u', '-m', 'crypto_options_bot', 'operator')
Write-Host "Launching: $Py $($argsList -join ' ')"
Write-Host "  stdout: $Stdout"
Write-Host "  stderr: $Stderr"

$proc = Start-Process -FilePath $Py `
                     -ArgumentList $argsList `
                     -WorkingDirectory $ProjectDir `
                     -RedirectStandardOutput $Stdout `
                     -RedirectStandardError $Stderr `
                     -NoNewWindow `
                     -PassThru
Write-Host "  PID = $($proc.Id)"
Set-Content -Path $PidFile -Value $proc.Id
