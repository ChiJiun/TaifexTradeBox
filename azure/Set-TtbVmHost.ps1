<#
.SYNOPSIS
Prepare the Windows VM so TTB keeps running unattended across market hours.

.DESCRIPTION
Run on the VM as Administrator (or through `az vm run-command invoke`). Default
is a read-only report; -Apply makes these changes:
  * Windows Update may download but never auto-reboots while a user is logged
    on (an unplanned reboot logs TTB out mid-session).
  * Time zone is Taipei Standard Time (session schedule and calendar rely on it).
  * Server Manager does not open at logon (keeps the TTB desktop uncluttered).
Windows auto-logon is reported but NOT configured here, because it needs the
VM password: run Sysinternals Autologon (https://learn.microsoft.com/sysinternals/downloads/autologon)
yourself; it stores the password as an encrypted LSA secret, not in plain text.
#>
param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$auKey = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$serverManager = 'HKLM:\SOFTWARE\Microsoft\ServerManager'

function Get-Value([string]$Path, [string]$Name) {
    (Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction SilentlyContinue).$Name
}

if ($Apply) {
    New-Item -Path $auKey -Force | Out-Null
    Set-ItemProperty -LiteralPath $auKey -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord
    Set-ItemProperty -LiteralPath $auKey -Name AUOptions -Value 3 -Type DWord  # download, notify to install
    if ((Get-TimeZone).Id -ne 'Taipei Standard Time') { Set-TimeZone -Id 'Taipei Standard Time' }
    if (Test-Path -LiteralPath $serverManager) {
        Set-ItemProperty -LiteralPath $serverManager -Name DoNotOpenServerManagerAtLogon -Value 1 -Type DWord
    }
}

[ordered]@{
    applied = [bool]$Apply
    timeZone = (Get-TimeZone).Id
    noAutoRebootWithLoggedOnUsers = Get-Value $auKey 'NoAutoRebootWithLoggedOnUsers'
    auOptions = Get-Value $auKey 'AUOptions'
    autoAdminLogon = Get-Value $winlogon 'AutoAdminLogon'
    autoLogonUser = Get-Value $winlogon 'DefaultUserName'
    interactiveSession = [bool](Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue)
    ttbRunning = [bool](Get-Process -Name TTB -ErrorAction SilentlyContinue)
    lastBoot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')
} | ConvertTo-Json
