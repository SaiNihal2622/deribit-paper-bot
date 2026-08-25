# Install Windows Task Scheduler entries for crypto-options-bot.
# Creates two tasks (no admin required for current-user tasks):
#   - CryptoOptionsBot\Heartbeat  : every 5 min, runs heartbeat.ps1
#   - CryptoOptionsBot\Watchdog   : at logon, runs watchdog.ps1
#   - CryptoOptionsBot\DailyReset : daily 00:05, runs daily_reset.ps1
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
$TaskFolder = "\CryptoOptionsBot"

function Remove-FolderIfEmpty {
    param([string]$Path)
    $f = Get-ScheduledTask -TaskPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $f) {
        # try removing folder
        $null = schtasks /Delete /TN "${Path}\__noop" /F 2>&1
    }
}

if ($Uninstall) {
    Write-Host "Uninstalling scheduled tasks under $TaskFolder ..."
    $tasks = Get-ScheduledTask -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    if ($tasks) {
        foreach ($t in $tasks) {
            Write-Host "  removing $($t.TaskName)"
            Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false
        }
    } else {
        Write-Host "  no tasks found"
    }
    Write-Host "Done."
    exit 0
}

# Create folder
$null = schtasks /Create /SC ONCE /TN "${TaskFolder}\__noop" /TR "cmd /c exit 0" /ST 00:00 /F 2>&1
$null = Unregister-ScheduledTask -TaskName "__noop" -TaskPath $TaskFolder -Confirm:$false -ErrorAction SilentlyContinue

# 1) Watchdog at logon
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\watchdog.ps1`"" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Watchdog" -TaskPath $TaskFolder -Action $action -Trigger $trigger -Settings $settings -Description "Crypto-options-bot watchdog: monitors bot + dashboard, auto-restarts" -Force | Out-Null
Write-Host "  installed: ${TaskFolder}\Watchdog  (at logon)"

# 2) Heartbeat every 5 min
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\heartbeat.ps1`" >> `"$ProjectDir\logs\heartbeat.out.log`" 2>&1" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Heartbeat" -TaskPath $TaskFolder -Action $action -Trigger $trigger -Settings $settings -Description "Crypto-options-bot heartbeat: every 5 min health check + log scan" -Force | Out-Null
Write-Host "  installed: ${TaskFolder}\Heartbeat  (every 5 min)"

# 3) Daily reset at 00:05
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectDir\daily_reset.ps1`" >> `"$ProjectDir\logs\daily_reset.out.log`" 2>&1" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Daily -At "00:05"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "DailyReset" -TaskPath $TaskFolder -Action $action -Trigger $trigger -Settings $settings -Description "Crypto-options-bot daily housekeeping: archive CSVs, rotate large logs" -Force | Out-Null
Write-Host "  installed: ${TaskFolder}\DailyReset  (daily 00:05)"

Write-Host "Done. View with: Get-ScheduledTask -TaskPath $TaskFolder"
