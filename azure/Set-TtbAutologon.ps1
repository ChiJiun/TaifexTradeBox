<#
.SYNOPSIS
Configure Windows auto-logon for the TTB VM with Sysinternals Autologon.

.DESCRIPTION
Run ON THE VM in an elevated PowerShell. It downloads Autologon from Microsoft,
verifies the Authenticode signature, asks for the Windows password (credential dialog)
input) and stores it as an encrypted LSA secret, so the VM logs the user in
after every reboot and the TTB Automation logon trigger starts TTB.
The password is never written to disk by this script. Any plain-text
DefaultPassword left in the Winlogon registry key is removed.
#>
param(
    [string]$UserName = 'azureuser',
    [string]$Domain = $env:COMPUTERNAME
)

$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')) {
    throw 'Run this from an elevated (Administrator) PowerShell.'
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$work = Join-Path $env:TEMP 'autologon'
New-Item -ItemType Directory -Path $work -Force | Out-Null
$zip = Join-Path $work 'AutoLogon.zip'
Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/AutoLogon.zip' -OutFile $zip -UseBasicParsing
Expand-Archive -LiteralPath $zip -DestinationPath $work -Force
$exe = Join-Path $work 'Autologon64.exe'
$sig = Get-AuthenticodeSignature $exe
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
    throw "unexpected Autologon signature: $($sig.Status) $($sig.SignerCertificate.Subject)"
}

# A credential dialog accepts Ctrl+V; Read-Host -AsSecureString in an RDP
# console session keeps only the first pasted character.
$credential = Get-Credential -UserName "$Domain\$UserName" -Message "Windows password for $Domain\$UserName (used for auto-logon)"
if (-not $credential) { throw 'Cancelled; nothing was changed.' }
$secure = $credential.Password
$plain = $credential.GetNetworkCredential().Password
try {
    # A wrong (e.g. half-pasted) password would leave the VM stuck at the logon
    # screen after the next reboot, so check it before storing it.
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $machine = New-Object System.DirectoryServices.AccountManagement.PrincipalContext('Machine')
    if (-not $machine.ValidateCredentials($UserName, $plain)) {
        throw "Password rejected for $Domain\$UserName ($($secure.Length) characters received); nothing was changed."
    }
    # Autologon64.exe is a GUI-subsystem binary: `&` would not wait or set $LASTEXITCODE.
    # Start-Process joins arguments verbatim; quote them so generated passwords
    # with spaces or quotes reach Autologon intact.
    $quoted = ($UserName, $Domain, $plain | ForEach-Object { '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }) -join ' '
    $process = Start-Process -FilePath $exe -ArgumentList "/accepteula $quoted" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Autologon exited with $($process.ExitCode)" }
} finally {
    $plain = $null
    [GC]::Collect()
}
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Remove-ItemProperty -LiteralPath $winlogon -Name DefaultPassword -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
$state = Get-ItemProperty -LiteralPath $winlogon
[ordered]@{
    autoAdminLogon = $state.AutoAdminLogon
    defaultUserName = $state.DefaultUserName
    defaultDomainName = $state.DefaultDomainName
    plainTextPasswordPresent = [bool]$state.DefaultPassword
    note = 'Password stored as LSA secret (DefaultPassword). Reboot to verify: TTB should appear on the desktop within ~2 minutes.'
} | ConvertTo-Json
