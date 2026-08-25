# Stop the crypto-options-bot (NSSM service or plain detached process).
# Usage:  .\stop_bot_service.ps1

$ErrorActionPreference = 'SilentlyContinue'

$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$NSSM = Join-Path $ProjectDir "tools\nssm.exe"
$BotService = "CryptoOptionsBot"

# Try NSSM first
if (Test-Path $NSSM) {
    $svc = Get-Service $BotService -ErrorAction SilentlyContinue
    if ($null -ne $svc) {
        & $NSSM stop $BotService
        Write-Host "NSSM stop $BotService issued"
    }
}

# Kill any leftover python crypto_options_bot processes
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like '*crypto_options_bot*'
}
foreach ($p in $procs) {
    Write-Host "Killing PID=$($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Host "Done."
