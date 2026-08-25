# NSSM service installer for crypto-options-bot
# Usage:  .\start_bot_service.ps1 install   (requires Admin)
#         .\start_bot_service.ps1 remove
#         .\start_bot_service.ps1 status
#         .\start_bot_service.ps1 install-dashboard
#
# Mirrors kotak-neo-bot's start_bot_service.ps1.  Service names are
# CryptoOptionsBot (paper) and CryptoOptionsDashboard (port 8511).

param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('install','remove','status','install-dashboard','remove-dashboard')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$ProjectDir  = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$VenvPython  = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$LogDir      = Join-Path $ProjectDir "logs"
$NSSMDir     = Join-Path $ProjectDir "tools"
$NSSM        = Join-Path $NSSMDir "nssm.exe"
$BotService  = "CryptoOptionsBot"
$DashService = "CryptoOptionsDashboard"

function Ensure-NSSM {
    if (-not (Test-Path $NSSM)) {
        Write-Host "Downloading NSSM (portable)..."
        if (-not (Test-Path $NSSMDir)) {
            New-Item -ItemType Directory -Force -Path $NSSMDir | Out-Null
        }
        $NssmZip = Join-Path $NSSMDir "nssm.zip"
        # NSSM 2.24 from official mirror
        Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $NssmZip -UseBasicParsing
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $Zip = [IO.Compression.ZipFile]::OpenRead($NssmZip)
        $Entry = $Zip.Entries | Where-Object { $_.FullName -like "*win64*" -and $_.Name -eq "nssm.exe" } | Select-Object -First 1
        if ($null -eq $Entry) { $Entry = $Zip.Entries | Where-Object { $_.Name -eq "nssm.exe" } | Select-Object -First 1 }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($Entry, $NSSMDir, $true)
        $Zip.Dispose()
        Remove-Item $NssmZip -Force
        if (-not (Test-Path $NSSM)) { throw "NSSM extraction failed" }
    }
    Write-Host "NSSM at: $NSSM"
}

function Install-BotService {
    Ensure-NSSM
    $Stdout = Join-Path $LogDir "bot_stdout.log"
    $Stderr = Join-Path $LogDir "bot_stderr.log"
    & $NSSM stop $BotService 2>$null
    & $NSSM remove $BotService confirm 2>$null
    Start-Sleep 1
    & $NSSM install $BotService $VenvPython "-u -m crypto_options_bot supervisor paper"
    & $NSSM set $BotService AppDirectory $ProjectDir
    & $NSSM set $BotService AppStdout $Stdout
    & $NSSM set $BotService AppStderr $Stderr
    & $NSSM set $BotService AppRotateFiles 1
    & $NSSM set $BotService AppRotateBytes 10485760
    & $NSSM set $BotService DisplayName "Crypto Options Paper Bot"
    & $NSSM set $BotService Description "Deribit testnet crypto options paper bot (5 strategies, real-time WS feed, stdlib dashboard, Telegram alerts)"
    & $NSSM set $BotService Start SERVICE_AUTO_START
    & $NSSM set $BotService AppRestartDelay 5000
    & $NSSM set $BotService AppThrottle 10000
    & $NSSM set $BotService ExitActions Restart
    & $NSSM set $BotService AppEnvironmentExtra "PYTHONPATH=$ProjectDir`nPYTHONUNBUFFERED=1"
    & $NSSM start $BotService
    Write-Host "Bot service installed and started. Use 'Get-Service $BotService' to check status."
}

function Install-DashboardService {
    Ensure-NSSM
    $Stdout = Join-Path $LogDir "dashboard_stdout.log"
    $Stderr = Join-Path $LogDir "dashboard_stderr.log"
    & $NSSM stop $DashService 2>$null
    & $NSSM remove $DashService confirm 2>$null
    Start-Sleep 1
    & $NSSM install $DashService $VenvPython "-u -m crypto_options_bot paper --dashboard-port 8511"
    & $NSSM set $DashService AppDirectory $ProjectDir
    & $NSSM set $DashService AppStdout $Stdout
    & $NSSM set $DashService AppStderr $Stderr
    & $NSSM set $DashService AppRotateFiles 1
    & $NSSM set $DashService AppRotateBytes 10485760
    & $NSSM set $DashService DisplayName "Crypto Options Dashboard (:8511)"
    & $NSSM set $DashService Description "Stdlib http.server dashboard for crypto-options-bot, port 8511"
    & $NSSM set $DashService Start SERVICE_AUTO_START
    & $NSSM set $DashService AppRestartDelay 5000
    & $NSSM set $DashService AppThrottle 10000
    & $NSSM set $DashService ExitActions Restart
    & $NSSM set $DashService AppEnvironmentExtra "PYTHONPATH=$ProjectDir`nPYTHONUNBUFFERED=1"
    & $NSSM start $DashService
    Write-Host "Dashboard service installed and started on :8511"
}

function Remove-AllServices {
    Ensure-NSSM
    foreach ($svc in @($BotService, $DashService)) {
        & $NSSM stop $svc 2>$null
        & $NSSM remove $svc confirm 2>$null
        Write-Host "Removed service: $svc"
    }
}

function Show-Status {
    foreach ($svc in @($BotService, $DashService)) {
        $s = Get-Service $svc -ErrorAction SilentlyContinue
        if ($null -eq $s) { Write-Host "$svc : NOT INSTALLED" }
        else { Write-Host "$svc : $($s.Status) (StartType: $($s.StartType))" }
    }
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like '*crypto_options_bot*'
    }
    Write-Host "Python procs (crypto_options_bot): $($procs.Count)"
    $procs | ForEach-Object {
        $age = (Get-Date) - $_.CreationDate
        $cmd = $_.CommandLine
        $preview = if ($cmd.Length -gt 90) { $cmd.Substring(0,90) + '...' } else { $cmd }
        Write-Host ("  PID={0} age={1}m cmd={2}" -f $_.ProcessId, [int]$age.TotalMinutes, $preview)
    }
}

switch ($Action) {
    'install'           { Install-BotService; Show-Status }
    'install-dashboard' { Install-DashboardService; Show-Status }
    'remove'            { Remove-AllServices }
    'remove-dashboard'  {
        Ensure-NSSM
        & $NSSM stop $DashService 2>$null
        & $NSSM remove $DashService confirm 2>$null
        Write-Host "Removed service: $DashService"
    }
    'status'            { Show-Status }
}
