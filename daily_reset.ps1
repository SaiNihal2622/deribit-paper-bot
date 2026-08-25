# Daily reset / housekeeping for crypto-options-bot
# - Archives yesterday's trade_events.csv and pnl_history.csv to logs/archive/
# - Truncates the active CSVs (paper state and trades_state.json preserved)
# - Optional: invoke `python -m crypto_options_bot reset` to wipe paper state
#
# Schedule with Windows Task Scheduler: daily at 00:05 IST.
# Mirrors kotak-neo-bot's housekeeping tasks.

param(
    [switch]$WipeState
)

$ErrorActionPreference = 'Stop'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"
$LogDir     = Join-Path $ProjectDir "logs"
$ArchiveDir = Join-Path $LogDir "archive"
$VenvPy     = Join-Path $ProjectDir ".venv\Scripts\python.exe"

if (-not (Test-Path $ArchiveDir)) { New-Item -ItemType Directory -Force -Path $ArchiveDir | Out-Null }

$ts = Get-Date -Format 'yyyyMMdd'
$tsHuman = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

function Archive-File {
    param([string]$Path)
    if (Test-Path $Path) {
        $base = Split-Path $Path -Leaf
        $dest = Join-Path $ArchiveDir ("$ts-$base")
        Move-Item -Path $Path -Destination $dest -Force
        Write-Host ("  archived: {0} -> {1}" -f $Path, $dest)
    } else {
        Write-Host ("  {0} : not found, skipping" -f $Path)
    }
}

Write-Host "=== Daily housekeeping $tsHuman ==="

# Archive CSVs
Archive-File (Join-Path $LogDir "trade_events.csv")
Archive-File (Join-Path $LogDir "pnl_history.csv")

# Rotate large log files (> 20 MB) by renaming them aside
foreach ($lg in @("bot.log", "bot_stderr.log", "bot_stdout.log", "supervisor.log", "watchdog.log")) {
    $p = Join-Path $LogDir $lg
    if ((Test-Path $p) -and ((Get-Item $p).Length -gt 20MB)) {
        $dest = Join-Path $ArchiveDir ("$ts-$lg")
        Move-Item -Path $p -Destination $dest -Force
        Write-Host ("  rotated: {0} -> {1}" -f $p, $dest)
    }
}

# Optional: wipe paper state
if ($WipeState) {
    Write-Host "WipeState requested - invoking reset"
    Set-Location $ProjectDir
    & $VenvPy -u -m crypto_options_bot reset
} else {
    Write-Host "  paper state preserved (use -WipeState to clear)"
}

Write-Host "=== Done ==="
