#!/usr/bin/env pwsh
# daily_backtest.sh — refresh the backtest report every morning
# Wired into the supervisor (CryptoSupervisor scheduled task) so the
# bot continuously validates its own edge against buy-and-hold.

$ErrorActionPreference = 'Stop'
Set-Location 'C:\Users\saini\.minimax-agent\projects\crypto-options-bot'

$out = "logs\daily_backtest.log"
"=== daily_backtest " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " ===" | Out-File $out -Encoding utf8

try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "python.exe"
    $psi.Arguments = "scripts\backtest.py --days 90"
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit(120000)
    if ($proc.ExitCode -eq 0) {
        "OK exit=0" | Out-File $out -Append
        $stdout.Split("`n") | Where-Object { $_ -match 'P&L|Sharpe|Trade|Period' } | ForEach-Object { "  $_" | Out-File $out -Append }
    } else {
        "FAIL exit=$($proc.ExitCode)" | Out-File $out -Append
        $stderr.Split("`n") | Select-Object -First 10 | ForEach-Object { "  err: $_" | Out-File $out -Append }
    }
} catch {
    "EXC: $_" | Out-File $out -Append
}
"=== done ===" | Out-File $out -Append
