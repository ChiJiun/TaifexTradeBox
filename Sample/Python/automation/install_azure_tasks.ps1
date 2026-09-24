param(
    [string]$RunAsUser = 'azureuser',
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$TtbExe = 'C:\TTB\TTB.exe'
)

$ErrorActionPreference = 'Stop'
$automation = Join-Path $WorkDir 'automation\run_ttb_automation.ps1'
if (-not (Test-Path -LiteralPath $automation)) {
    throw "Automation runner not found: $automation"
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

Register-ScheduledTask -TaskName 'TTB GUI' -Action $ttbAction -Trigger $logonTrigger -Principal $principal -Settings $settings -Force | Out-Null
Register-ScheduledTask -TaskName 'TTB Automation' -Action $botAction -Trigger @($logonTrigger, $dailyTrigger) -Principal $principal -Settings $settings -Force | Out-Null

Get-ScheduledTask -TaskName 'TTB GUI', 'TTB Automation' |
    Select-Object TaskName, State, @{Name='RunAs';Expression={$_.Principal.UserId}}
