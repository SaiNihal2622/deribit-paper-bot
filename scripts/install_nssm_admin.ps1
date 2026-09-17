# Crypto Options Bot — admin install for NSSM service tier (reboot-survival).
#
# Paste this into an ADMIN PowerShell:
#   1. Press Win+X, "Windows PowerShell (Admin)"  or  "Terminal (Admin)"
#   2. Run:
#        cd "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
#        powershell -ExecutionPolicy Bypass -File scripts\install_nssm_admin.ps1
#
# What it does:
#   - Configures the two already-registered NSSM services
#     (CryptoOptionsBot + CryptoOptionsOperator) with proper working
#     directory, log rotation, restart policy.
#   - Starts them.
#   - Confirms they're running.
#
# After this, the system survives crashes AND reboots (SERVICE_AUTO_START).

$ErrorActionPreference = 'Stop'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$NSSM       = Join-Path $ProjectDir "tools\nssm.exe"
$PythonExe  = "C:\Program Files\Python312\python.exe"

if (-not (Test-Path $NSSM)) {
    Write-Host "NSSM not found at $NSSM" -ForegroundColor Red
    Write-Host "Download from https://nssm.cc/release/nssm-2.24.zip and place nssm.exe into $ProjectDir\tools\"
    exit 1
}

function Configure-Service {
    param([string]$Name, [string]$DisplayName, [string]$Description,
          [string]$Stdout, [string]$Stderr)

    Write-Host "Configuring $Name ..." -ForegroundColor Cyan
    & $NSSM set $Name AppDirectory   $ProjectDir                  | Out-Null
    & $NSSM set $Name AppStdout      $Stdout                       | Out-Null
    & $NSSM set $Name AppStderr      $Stderr                       | Out-Null
    & $NSSM set $Name AppRotateFiles 1                             | Out-Null
    & $NSSM set $Name AppRotateBytes 10485760                      | Out-Null
    & $NSSM set $Name DisplayName    $DisplayName                  | Out-Null
    & $NSSM set $Name Description    $Description                  | Out-Null
    & $NSSM set $Name Start          SERVICE_AUTO_START             | Out-Null
    & $NSSM set $Name AppRestartDelay 5000                         | Out-Null
    & $NSSM set $Name AppExit        Default Restart                | Out-Null
    Write-Host "  done."
}

# If services don't exist yet, create them.
if (-not (Get-Service CryptoOptionsBot -ErrorAction SilentlyContinue)) {
    Write-Host "Creating CryptoOptionsBot service ..." -ForegroundColor Cyan
    & $NSSM install CryptoOptionsBot $PythonExe "-u -m crypto_options_bot paper --feed ws --dashboard-port 8511" | Out-Null
}
if (-not (Get-Service CryptoOptionsOperator -ErrorAction SilentlyContinue)) {
    Write-Host "Creating CryptoOptionsOperator service ..." -ForegroundColor Cyan
    & $NSSM install CryptoOptionsOperator $PythonExe "-u -m crypto_options_bot operator" | Out-Null
}

Configure-Service `
    -Name "CryptoOptionsBot" `
    -DisplayName "Crypto Options Paper Bot" `
    -Description "Deribit testnet paper trading bot. 5-strategy options core." `
    -Stdout (Join-Path $ProjectDir "logs\bot_stdout.log") `
    -Stderr (Join-Path $ProjectDir "logs\bot_stderr.log")

Configure-Service `
    -Name "CryptoOptionsOperator" `
    -DisplayName "Crypto Options 6-Agent Operator" `
    -Description "Sentinel+Healer+Trader+Evolver+Reflector. Self-evolving 24/7 LLM-driven agent layer." `
    -Stdout (Join-Path $ProjectDir "logs\operator_stdout.log") `
    -Stderr (Join-Path $ProjectDir "logs\operator_stderr.log")

# Stop any detached instances so the services can own the ports.
$botPidFile    = Join-Path $ProjectDir "logs\bot.pid"
$opPidFile     = Join-Path $ProjectDir "logs\operator.pid"
foreach ($f in @($botPidFile, $opPidFile)) {
    if (Test-Path $f) {
        $p = Get-Content $f -ErrorAction SilentlyContinue
        if ($p -and (Get-Process -Id $p -ErrorAction SilentlyContinue)) {
            Write-Host "Stopping detached process PID=$p ..." -ForegroundColor Yellow
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
    }
}
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "Starting services ..." -ForegroundColor Cyan
Start-Service CryptoOptionsBot
Start-Service CryptoOptionsOperator
Start-Sleep -Seconds 5

Write-Host ""
Write-Host "Service status:" -ForegroundColor Green
Get-Service | Where-Object { $_.Name -match "CryptoOptions" } | Format-Table Name, Status, StartType -AutoSize

Write-Host "Dashboard:    http://127.0.0.1:8511/" -ForegroundColor Green
Write-Host "Status tool:  python scripts\operator_status.py" -ForegroundColor Green
Write-Host ""
Write-Host "DONE. Reboot-survival: YES (SERVICE_AUTO_START)." -ForegroundColor Green
