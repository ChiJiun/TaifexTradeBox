param(
    [string]$RunAsUser = 'azureuser',
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$TtbExe = 'C:\TTB\TTB.exe'
)

$ErrorActionPreference = 'Stop'
$automation = Join-Path $WorkDir 'automation\run_ttb_automation.ps1'
$watchdog = Join-Path $WorkDir 'automation\health_watchdog.ps1'
if (-not (Test-Path -LiteralPath $automation)) {
    throw "Automation runner not found: $automation"
}
if (-not (Test-Path -LiteralPath $watchdog)) {
    throw "Health watchdog not found: $watchdog"
}

$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAsUser
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At '08:35'
$ttbAction = New-ScheduledTaskAction -Execute $TtbExe
$botAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$automation`"" `
    -WorkingDirectory $WorkDir
$watchdogAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watchdog`"" `
    -WorkingDirectory $WorkDir
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

Register-ScheduledTask -TaskName 'TTB GUI' -Action $ttbAction -Trigger $logonTrigger -Principal $principal -Settings $settings -Force | Out-Null
Register-ScheduledTask -TaskName 'TTB Automation' -Action $botAction -Trigger @($logonTrigger, $dailyTrigger) -Principal $principal -Settings $settings -Force | Out-Null
Register-ScheduledTask -TaskName 'TTB Health Watchdog' -Action $watchdogAction -Trigger $watchdogTrigger -Principal $principal -Settings $settings -Force | Out-Null

Get-ScheduledTask -TaskName 'TTB GUI', 'TTB Automation', 'TTB Health Watchdog' |
    Select-Object TaskName, State, @{Name='RunAs';Expression={$_.Principal.UserId}}
