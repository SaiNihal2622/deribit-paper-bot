# crypto_supervisor_loop.ps1 — SYSTEM-context watchdog for the crypto-options-bot.
#
# The "always on" layer that runs even if the user is away. Every 30 seconds:
#   1. NSSM service CryptoOptionsBot — start if not RUNNING
#   2. Liveness freshness (data_cache/liveness.json age < 180s) — restart bot via NSSM
#   3. Bot process alive (separate check) — restart if PID dead
#   4. Orphan-killer sweep every 15 min — kill any zombie python whose cmdline
#      matches crypto_options_bot but whose PID doesn't match the live one
#
# Runs as a SYSTEM scheduled task via CryptoSupervisor (registered by
# install_crypto_supervisor.py using the force-action JSON trick). Survives
# user logoff and machine sleep; only dies on full reboot, in which case the
# task fires at system startup.
#
# Logs to logs/supervisor_loop.log.
#
# Mirrors kotak-neo-bot/scripts/supervisor_loop.ps1 pattern.

$ErrorActionPreference = 'Continue'

$ROOT       = 'C:\Users\saini\.minimax-agent\projects\crypto-options-bot'
$LOG_DIR    = Join-Path $ROOT 'logs'
$LOG_FILE   = Join-Path $LOG_DIR 'supervisor_loop.log'
$NSSM_EXE   = 'C:\Tools\nssm\nssm-2.24\win64\nssm.exe'
$PYTHON_EXE = 'C:\Program Files\Python312\python.exe'

$SERVICE_BOT       = 'CryptoOptionsBot'
$CHECK_INTERVAL_SEC  = 30
$STALE_THRESHOLD_SEC = 180          # liveness older than this = bot is wedged
$ORPHAN_KILL_EVERY   = 30           # every 30 cycles (~15 min) sweep for zombies

if (-not (Test-Path $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
}

function Log([string]$msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
}

function Get-NssmStatus([string]$service) {
    try {
        $out = & sc.exe query $service 2>&1
        if ($LASTEXITCODE -ne 0) { return 'UNKNOWN' }
        $stdout = ($out | Out-String).ToUpper()
        if ($stdout -match 'STATE\s+:\s+\d+\s+RUNNING')        { return 'RUNNING' }
        if ($stdout -match 'STATE\s+:\s+\d+\s+STOPPED')        { return 'STOPPED' }
        if ($stdout -match 'STATE\s+:\s+\d+\s+START_PENDING')  { return 'START_PENDING' }
        if ($stdout -match 'STATE\s+:\s+\d+\s+STOP_PENDING')   { return 'STOP_PENDING' }
        return 'UNKNOWN'
    } catch {
        return 'UNKNOWN'
    }
}

function Start-NssmService([string]$service) {
    try {
        Log "[supervisor] starting NSSM service $service"
        $p = Start-Process -FilePath $NSSM_EXE -ArgumentList @('start', $service) -Wait -PassThru -NoNewWindow
        if ($p.ExitCode -eq 0) {
            Log "[supervisor] $service started OK (exit=0)"
            return $true
        } else {
            Log "[supervisor] $service start failed (exit=$($p.ExitCode))"
            return $false
        }
    } catch {
        Log "[supervisor] $service start error: $_"
        return $false
    }
}

function Restart-BotViaNssm() {
    try {
        Log "[supervisor] restarting $SERVICE_BOT via NSSM (liveness stale or dead)"
        # Clean up orphan zombies FIRST so NSSM restart doesn't wedge on file locks
        & $PYTHON_EXE (Join-Path $ROOT 'scripts\crypto_orphan_killer.py') --clean 2>&1 | Out-Null
        $p = Start-Process -FilePath $NSSM_EXE -ArgumentList @('restart', $SERVICE_BOT) -Wait -PassThru -NoNewWindow
        if ($p.ExitCode -eq 0) {
            Log "[supervisor] restart OK (exit=0)"
            return $true
        } else {
            Log "[supervisor] restart exit=$($p.ExitCode)"
            return $false
        }
    } catch {
        Log "[supervisor] restart error: $_"
        return $false
    }
}

function Get-BotPid() {
    $liv = Join-Path $ROOT 'data_cache\liveness.json'
    if (-not (Test-Path $liv)) { return $null }
    try {
        $d = Get-Content $liv -Raw | ConvertFrom-Json
        $proc_id = $d.pid
        if ($null -eq $proc_id) { return $null }
        return [int]$proc_id
    } catch {
        return $null
    }
}

function Get-BotLivenessAge() {
    $liv = Join-Path $ROOT 'data_cache\liveness.json'
    if (-not (Test-Path $liv)) { return 999999 }
    try {
        $d = Get-Content $liv -Raw | ConvertFrom-Json
        if (-not $d.ts) { return 999999 }
        $last = [datetime]$d.ts
        $diff = ([datetime]::UtcNow - $last.ToUniversalTime()).TotalSeconds
        return $diff
    } catch {
        return 999999
    }
}

function Test-BotProcessAlive([int]$proc_id) {
    if ($proc_id -le 0) { return $false }
    try {
        $p = Get-Process -Id $proc_id -ErrorAction SilentlyContinue
        return ($null -ne $p -and -not $p.HasExited)
    } catch {
        return $false
    }
}

function Run-Cycle($cycle) {
    Log "[supervisor] cycle=$cycle start"
    # 1. NSSM service state
    $svc = Get-NssmStatus $SERVICE_BOT
    if ($svc -ne 'RUNNING') {
        Log "[supervisor] cycle=$cycle $SERVICE_BOT nssm=$svc - starting"
        $ok = Start-NssmService $SERVICE_BOT
        if (-not $ok) {
            Log "[supervisor] cycle=$cycle NSSM start failed - falling back to direct python start"
            # Spawn user-mode python as the current supervisor context (SYSTEM).
            # Same path the NSSM service would use, but without the wrapper.
            $arglist = @('-u', '-m', 'crypto_options_bot', 'paper', '--feed', 'ws',
                         '--recover-orphan', '--dashboard-port', '8511')
            Start-Process -FilePath $PYTHON_EXE -ArgumentList $arglist `
                          -WorkingDirectory $ROOT -WindowStyle Hidden
        }
        return  # let next cycle check liveness
    }

    # 2. Liveness freshness — bot must update liveness.json every ~60s
    $age = Get-BotLivenessAge
    $bot_pid = Get-BotPid
    if ($age -gt $STALE_THRESHOLD_SEC) {
        Log "[supervisor] cycle=$cycle liveness stale (age=[int]$age s, pid=$bot_pid) - restarting bot"
        Restart-BotViaNssm
        return
    }

    # 3. Bot process actually alive
    if ($bot_pid -and -not (Test-BotProcessAlive $bot_pid)) {
        Log "[supervisor] cycle=$cycle PID $bot_pid not running but liveness says alive - restarting"
        Restart-BotViaNssm
        return
    }

    # 4. Periodic orphan sweep — kill any crypto_options_bot python whose PID
    #    doesn't match the live one. Catches the zombie pattern we saw with
    #    pid 22168 (a dead nssm child surviving a kill).
    if (($cycle % $ORPHAN_KILL_EVERY) -eq 0) {
        Log "[supervisor] cycle=$cycle orphan sweep"
        & $PYTHON_EXE (Join-Path $ROOT 'scripts\crypto_orphan_killer.py') --clean 2>&1 | Out-Null
    }

    # 5. Verbose status every 10 min
    if (($cycle % 20) -eq 0) {
        $pid_str = if ($bot_pid) { $bot_pid.ToString() } else { 'none' }
        Log "[supervisor] cycle=$cycle OK | nssm=$svc | pid=$pid_str | liveness_age=[int]$age s"
    }
}

# Main loop
Log "=================================================="
Log "[supervisor] starting (interval=$CHECK_INTERVAL_SEC s, stale=$STALE_THRESHOLD_SEC s)"
$ctx = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Log "[supervisor] context: $ctx"

$cycle = 0
while ($true) {
    $cycle += 1
    try {
        Run-Cycle $cycle
    } catch {
        Log "[supervisor] cycle=$cycle exception: $_"
    }
    Start-Sleep -Seconds $CHECK_INTERVAL_SEC
}
