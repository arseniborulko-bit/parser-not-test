# Registers the Telegram subscription handler for the current Windows user.
# Run once from PowerShell in the project folder:
#   .\install_telegram_bot_autostart.ps1

$ErrorActionPreference = "Stop"
$taskName = "AmazonParserTelegramSubscriberBot"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $projectDir ".venv\Scripts\python.exe"
$botScript = Join-Path $projectDir "telegram_subscriber_bot.py"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Virtual-environment Python was not found: $pythonExe"
}

$action = New-ScheduledTaskAction -Execute $pythonExe -Argument ('"{0}"' -f $botScript) -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "Telegram /start subscription handler for Amazon Parser" -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host "Done. Task '$taskName' is running and will start automatically at Windows sign-in."
