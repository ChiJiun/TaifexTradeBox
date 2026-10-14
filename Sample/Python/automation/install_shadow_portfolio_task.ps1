param(
    [string]$RunAsUser = 'azureuser',
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$Manifest = 'C:\TTB\Sample\Python\automation\shadow_portfolio.json',
    [string]$TaskName = 'TTB Shadow Portfolio',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Manifest)) {
    throw "Missing manifest: $Manifest"
}
$python = Join-Path $WorkDir 'automation\shadow_portfolio.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Missing portfolio runner: $python" }
Push-Location $WorkDir
try {
    # Read-only validate and print generated per-instance paths; no launch.
    & py.exe -3.13 -m automation.shadow_portfolio --config $Manifest | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Shadow portfolio manifest validation failed' }
} finally {
    Pop-Location
}

if (-not $Apply) {
    Write-Warning "DRY RUN: use -Apply to register a shadow-only logon task named '$TaskName'"
    return
}
$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAsUser
$action = New-ScheduledTaskAction -Execute 'py.exe' -Argument ('-3.13 -m automation.shadow_portfolio --config "{0}" --run' -f $Manifest) -WorkingDirectory $WorkDir
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
