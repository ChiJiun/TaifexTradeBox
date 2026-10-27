<#
.SYNOPSIS
Install the runtimes a fresh Windows Server VM needs before TTB can run.

.DESCRIPTION
Runs ON the VM as Administrator (gcp/Deploy-TtbRuntime.ps1 -Bootstrap runs it over SSH).
Idempotent: each component is skipped when already present. Installs
  * .NET 10 NETCore, AspNetCore and WindowsDesktop runtimes (TTB 2026.07.08 needs all three),
  * Visual C++ 2015-2022 x64 runtime (pywinauto's win32ui needs mfc140u.dll),
  * Python 3.13 for all users with the py launcher.
Every installer is downloaded from Microsoft or python.org and must carry a valid
Authenticode signature from the expected publisher before it runs.
#>
param([string]$PythonVersion = '3.13.17')

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$download = Join-Path $env:TEMP 'ttb-bootstrap'
New-Item -ItemType Directory -Path $download -Force | Out-Null

function Install-Signed([string]$Url, [string]$Publisher, [string[]]$Arguments) {
    $file = Join-Path $download ([IO.Path]::GetFileName(([Uri]$Url).AbsolutePath))
    Invoke-WebRequest -Uri $Url -OutFile $file -UseBasicParsing
    $signature = Get-AuthenticodeSignature -LiteralPath $file
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike "*O=$Publisher*") {
        throw "untrusted installer $file ($($signature.Status), $($signature.SignerCertificate.Subject))"
    }
    $process = Start-Process -FilePath $file -ArgumentList $Arguments -Wait -PassThru
    # 3010 = success, reboot required.
    if ($process.ExitCode -notin 0, 3010) { throw "$file exited $($process.ExitCode)" }
    Remove-Item -LiteralPath $file -Force
}

function Test-DotnetRuntime([string]$Name) {
    [bool](Get-ChildItem "$env:ProgramFiles\dotnet\shared\$Name" -Filter '10.*' -ErrorAction SilentlyContinue)
}

$dotnet = [ordered]@{
    'Microsoft.NETCore.App'        = 'https://aka.ms/dotnet/10.0/dotnet-runtime-win-x64.exe'
    'Microsoft.AspNetCore.App'     = 'https://aka.ms/dotnet/10.0/aspnetcore-runtime-win-x64.exe'
    'Microsoft.WindowsDesktop.App' = 'https://aka.ms/dotnet/10.0/windowsdesktop-runtime-win-x64.exe'
}
foreach ($runtime in $dotnet.Keys) {
    if (-not (Test-DotnetRuntime $runtime)) {
        Install-Signed $dotnet[$runtime] 'Microsoft Corporation' @('/install', '/quiet', '/norestart')
    }
}
if (-not (Test-Path (Join-Path $env:WINDIR 'System32\mfc140u.dll'))) {
    Install-Signed 'https://aka.ms/vs/17/release/vc_redist.x64.exe' 'Microsoft Corporation' @('/install', '/quiet', '/norestart')
}
$pyLauncher = Join-Path $env:WINDIR 'py.exe'
$hasPython = (Test-Path $pyLauncher) -and ((& $pyLauncher -0 2>$null) -match '3\.13')
if (-not $hasPython) {
    Install-Signed "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" 'Python Software Foundation' @(
        '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_launcher=1', 'InstallLauncherAllUsers=1', 'Include_test=0'
    )
}

[ordered]@{
    dotnet10 = @($dotnet.Keys | Where-Object { Test-DotnetRuntime $_ }).Count -eq 3
    vcRuntime = Test-Path (Join-Path $env:WINDIR 'System32\mfc140u.dll')
    python = ((& $pyLauncher -3.13 --version 2>&1) | Out-String).Trim()
} | ConvertTo-Json
