# crypto-options-bot Heartbeat
# Health check for the bot (Python process), dashboard (port 8511), and
# log freshness.  No market-hours gating (crypto is 24/7).
#
# Mirrors kotak-neo-bot's heartbeat.ps1.

$ErrorActionPreference = 'Stop'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$DashboardPort = 8511

$ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Host "=== Heartbeat $ts | crypto-options-bot ==="
Set-Location $ProjectDir

# Step 1: alive check (path + 4h window)
$alive4 = (Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like '*crypto-options-bot*' -and $_.StartTime -gt (Get-Date).AddHours(-4)
}).Count
$allBot = (Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like '*crypto-options-bot*'
}).Count
Write-Host "alive4=$alive4 allBot=$allBot"

$procs = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*crypto-options-bot*' } | Select-Object Id, StartTime, @{N='AgeMin';E={[math]::Round(((Get-Date) - $_.StartTime).TotalMinutes,1)}}
if ($procs) { $procs | Format-Table -AutoSize | Out-String | Write-Host } else { Write-Host "  (no python processes match crypto-options-bot)" }

# Step 2: log error scan
Write-Host "--- ERROR SCAN ---"
foreach ($candidate in @("logs\bot_stderr.log", "logs\bot.log")) {
    if (Test-Path $candidate) {
        $errs = Select-String -Path $candidate -Pattern 'Traceback|FATAL|Killed|Exception' -ErrorAction SilentlyContinue | Select-Object -Last 3
        if ($errs) {
            $errs | ForEach-Object { Write-Host ("  [{0}] L{1}: {2}" -f $candidate, $_.LineNumber, $_.Line.Substring(0, [Math]::Min(200, $_.Line.Length))) }
        } else { Write-Host "  [$candidate] no errors" }
        $f = Get-Item $candidate
        Write-Host ("  {0} size={1} lastWrite={2}" -f $candidate, $f.Length, $f.LastWriteTime)
    } else {
        Write-Host ("  {0} : missing" -f $candidate)
    }
}

# Step 3: dashboard health (try /api/status first, fall back to /)
Write-Host "--- DASHBOARD HEALTH ---"
$dashCode = 0
try {
    $r = Invoke-WebRequest -Uri "http://localhost:$DashboardPort/api/status" -TimeoutSec 5 -UseBasicParsing
    $dashCode = [int]$r.StatusCode
    Write-Host "  /api/status HTTP $($r.StatusCode) | $($r.RawContentLength) bytes"
} catch {
    Write-Host "  /api/status error: $($_.Exception.Message)"
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$DashboardPort/" -TimeoutSec 5 -UseBasicParsing
        $dashCode = [int]$r.StatusCode
        Write-Host "  /  HTTP $($r.StatusCode) (fallback probe)"
    } catch {
        Write-Host "  /  error: $($_.Exception.Message)"
    }
}

# Step 4: state files exist
Write-Host "--- STATE FILES ---"
foreach ($f in @("data_cache\paper_state.json","data_cache\trades_state.json","logs\trade_events.csv","logs\pnl_history.csv")) {
    if (Test-Path $f) {
        $fi = Get-Item $f
        Write-Host ("  {0} size={1} mtime={2}" -f $f, $fi.Length, $fi.LastWriteTime)
    } else {
        Write-Host "  $f : missing"
    }
}

# Step 5: decision
Write-Host "--- DECISION ---"
if ($alive4 -eq 0 -and $allBot -eq 0) {
    Write-Host "  no bot process at all -> RESTART (invoke start_bot_detached.ps1)"
} elseif ($alive4 -eq 0 -and $allBot -gt 0) {
    Write-Host "  alive4=0 but allBot=$allBot -> old process, monitor only (no restart)"
} else {
    Write-Host "  bot alive (alive4=$alive4 allBot=$allBot) -> no restart"
}
Write-Host "=== END HEARTBEAT ==="
