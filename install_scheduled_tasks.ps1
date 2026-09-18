# Install Windows Task Scheduler entries for crypto-options-bot.
# Creates three user-level tasks (no admin required):
#   - CryptoOptionsBotWatchdog   : at logon, monitors + auto-restarts bot
#   - CryptoOptionsBotHeartbeat  : every 5 min, logs health snapshot
#   - CryptoOptionsBotDailyReset : daily 00:05, archives logs
#
# Usage:  .\install_scheduled_tasks.ps1
#         .\install_scheduled_tasks.ps1 -Uninstall
#
# Mirrors the kotak-neo-bot scheduled-task setup.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$ProjectDir = "C:\Users\saini\.minimax-agent\projects\crypto-options-bot"

# Task names are namespaced to avoid collisions with anything else
$WatchdogName  = "CryptoOptionsBotWatchdog"
$HeartbeatName = "CryptoOptionsBotHeartbeat"
$DailyName     = "CryptoOptionsBotDailyReset"
$AllTasks = @($WatchdogName, $HeartbeatName, $DailyName)

if ($Uninstall) {
    Write-Host "Uninstalling scheduled tasks..."
    foreach ($name in $AllTasks) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($t) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "  removed: $name"
        } else {
            Write-Host "  not found: $name"
        }
    }
    Write-Host "Done."
    exit 0
}

# Helper: register a task. Falls back gracefully if the registration fails
# (e.g., no admin, missing privilege).
function Install-Task {
    param(
        [string]$Name,
        [string]$ScriptRelative,
        [string]$TriggerSpec,    # "atlogon" | "every5min" | "every1min" | "daily0005"
        [string]$Description
    )
    $scriptPath = Join-Path $ProjectDir $ScriptRelative
    if (-not (Test-Path $scriptPath)) {
        Write-Warning "  SKIP $Name (script not found: $scriptPath)"
        return $false
    }

    $arg = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $ProjectDir
    switch ($TriggerSpec) {
        "atlogon"   { $trigger = New-ScheduledTaskTrigger -AtLogOn }
        "every5min" {
            $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                -RepetitionInterval (New-TimeSpan -Minutes 5) `
                -RepetitionDuration (New-TimeSpan -Days 3650)
        }
        "every1min" {
            # Watchdog runs every 1 min for tight crash recovery without
            # admin (AtLogOn triggers need elevation).
            $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
                -RepetitionInterval (New-TimeSpan -Minutes 1) `
                -RepetitionDuration (New-TimeSpan -Days 3650)
        }
        "daily0005" { $trigger = New-ScheduledTaskTrigger -Daily -At "00:05" }
        default     { throw "unknown trigger spec: $TriggerSpec" }
    }
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    try {
        # Remove existing first so we don't fight with prior runs.
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Description $Description -Force | Out-Null
        Write-Host "  installed: $Name ($TriggerSpec)"
        return $true
    } catch {
        Write-Warning "  FAILED $Name - $($_.Exception.Message)"
        return $false
    }
}

Write-Host "Installing scheduled tasks under user context..."
$ok = $true
# Watchdog: every-1-min repeating trigger. Survives reboot (Task Scheduler
# starts it within ~60s of machine boot) AND auto-restarts the bot if it
# dies. Same effect as AtLogOn but doesn't require admin.
$ok = (Install-Task -Name $WatchdogName -ScriptRelative "watchdog.ps1" -TriggerSpec "every1min" -Description "Crypto-options-bot watchdog: monitors bot + dashboard, auto-restarts every minute") -and $ok
$ok = (Install-Task -Name $HeartbeatName -ScriptRelative "heartbeat.ps1" -TriggerSpec "every5min" -Description "Crypto-options-bot heartbeat: every 5 min health check + log scan") -and $ok
$ok = (Install-Task -Name $DailyName -ScriptRelative "daily_reset.ps1" -TriggerSpec "daily0005" -Description "Crypto-options-bot daily housekeeping: archive CSVs, rotate large logs") -and $ok

Write-Host ""
if ($ok) {
    Write-Host "All 3 scheduled tasks installed."
    Write-Host "Verify with: Get-ScheduledTask -TaskName CryptoOptionsBot*"
} else {
    Write-Host "Some tasks failed. Verify with: Get-ScheduledTask -TaskName CryptoOptionsBot*"
    Write-Host "Re-run from an elevated (admin) PowerShell if you see access-denied errors."
}