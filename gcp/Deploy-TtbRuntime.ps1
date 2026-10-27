<#
.SYNOPSIS
Deploy a committed snapshot of this repo to the GCP TTB VM over SSH.

.DESCRIPTION
Default is a dry run that only builds and hashes the package. With -Apply it uploads a
`git archive` of the commit (Sample/Python, azure, gcp, deploy, icon, TTB.exe, NLog.config)
with `gcloud compute scp` and runs deploy/Install-TtbRuntime.ps1 on the VM, which
verifies the SHA256, stops the bot gracefully, backs up, copies, tests, rolls back on
failure and restarts TTB Automation. -Bootstrap first runs gcp/Bootstrap-TtbVm.ps1
(.NET 10, VC++ runtime, Python 3.13) and azure/Set-TtbVmHost.ps1 -Apply.
No GitHub credentials are stored on the VM. Only committed content is deployed.
#>
param(
    [string]$Project = 'ttb-competition',
    [string]$Zone = 'asia-east1-b',
    [string]$Instance = 'ttb-sim-gcp',
    [string]$SshUser = 'ttbuser',
    [string]$RunAsUser = 'ttbuser',
    [string]$Ref = 'HEAD',
    [string]$RemoteRoot = 'C:\TTB',
    [switch]$Bootstrap,
    [switch]$SkipTests,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$repo = (& git rev-parse --show-toplevel).Trim()
$commit = (& git -C $repo rev-parse --verify "$Ref^{commit}").Trim()
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$work = Join-Path $env:TEMP "ttb-deploy-$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$zip = Join-Path $work "$stamp.zip"
& git -C $repo archive --format=zip -o $zip $commit Sample/Python azure gcp deploy icon TTB.exe NLog.config
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
$sha = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash

[ordered]@{
    commit = $commit
    vm = "$Project/$Zone/$Instance"
    packageBytes = (Get-Item -LiteralPath $zip).Length
    sha256 = $sha
    bootstrap = [bool]$Bootstrap
    apply = [bool]$Apply
} | ConvertTo-Json
if (-not $Apply) {
    Write-Host 'Dry run only. Re-run with -Apply to upload and install on the VM.'
    exit 0
}
$dirty = (& git -C $repo status --porcelain -- Sample/Python azure gcp deploy | Out-String).Trim()
if ($dirty -and $Ref -eq 'HEAD') {
    Write-Warning 'Working tree has uncommitted changes; only the committed snapshot is deployed.'
}

$target = "$SshUser@$Instance"
$gcloudArgs = @('--project', $Project, '--zone', $Zone, '--quiet')
# Windows PowerShell 5.1 turns gcloud's stderr progress into terminating errors
# under 'Stop'; rely on exit codes for the remote steps instead.
$ErrorActionPreference = 'Continue'
function Invoke-Vm([string]$Command) {
    & gcloud compute ssh $target @gcloudArgs --command $Command
    if ($LASTEXITCODE -ne 0) { throw "remote command failed ($LASTEXITCODE): $Command" }
}
function Invoke-VmScript([string]$Path, [string]$Arguments = '') {
    Invoke-Vm "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path $Arguments"
}

$incoming = "$RemoteRoot\deploy-incoming"
$remote = $incoming.Replace('\', '/')
Invoke-Vm "if not exist $incoming mkdir $incoming"
foreach ($name in 'deploy/Install-TtbRuntime.ps1', 'gcp/Bootstrap-TtbVm.ps1', 'azure/Set-TtbVmHost.ps1') {
    $local = Join-Path $work ("$stamp-" + [IO.Path]::GetFileName($name))
    # Scripts come from the deployed commit, not the working tree.
    [IO.File]::WriteAllText($local, ((& git -C $repo show "${commit}:$name") -join "`r`n"))
    if ($LASTEXITCODE -ne 0) { throw "$name is not in the deployed commit" }
}
# gcloud compute scp (PuTTY pscp) and SFTP-based scp both drop the connection against
# the GCE Windows sshd; the legacy scp protocol (-O) of Windows' own OpenSSH works.
# `gcloud compute ssh` above already pushed the key to the instance metadata.
$ip = (& gcloud compute instances describe $Instance --project $Project --zone $Zone --format 'value(networkInterfaces[0].accessConfigs[0].natIP)').Trim()
$sshDir = Join-Path $env:USERPROFILE '.ssh'
$scpArgs = @('-O', '-q', '-i', (Join-Path $sshDir 'google_compute_engine'),
    '-o', 'StrictHostKeyChecking=accept-new', '-o', "UserKnownHostsFile=$(Join-Path $sshDir 'google_compute_known_hosts')")
foreach ($file in Get-ChildItem -LiteralPath $work -File) {
    & "$env:WINDIR\System32\OpenSSH\scp.exe" @scpArgs $file.FullName "$SshUser@${ip}:$remote/$($file.Name)"
    if ($LASTEXITCODE -ne 0) { throw "scp failed for $($file.Name)" }
}

if ($Bootstrap) {
    Write-Host 'Installing runtimes (.NET 10, VC++, Python 3.13)...'
    Invoke-VmScript "$incoming\$stamp-Bootstrap-TtbVm.ps1"
    Invoke-VmScript "$incoming\$stamp-Set-TtbVmHost.ps1" '-Apply'
}
Write-Host 'Installing on VM (unit tests may take a few minutes)...'
$skip = if ($SkipTests) { ' -SkipTests' } else { '' }
Invoke-VmScript "$incoming\$stamp-Install-TtbRuntime.ps1" "-Zip $incoming\$stamp.zip -Sha256 $sha -Root $RemoteRoot -Stamp $stamp -RunAs $RunAsUser$skip"
Invoke-Vm "del /q $incoming\$stamp-*.ps1"
