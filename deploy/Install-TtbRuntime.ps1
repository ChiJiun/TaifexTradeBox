<#
.SYNOPSIS
Install a git-archive package of this repo on the TTB VM (Azure or GCP).

.DESCRIPTION
Runs ON the VM as Administrator. Called by azure/Deploy-TtbRuntime.ps1 (through
Run Command) and gcp/Deploy-TtbRuntime.ps1 (through SSH). It:
  1. verifies the package SHA256,
  2. asks the running bot to stop gracefully (stop.requested) and stops the TTB Automation task,
  3. backs up <Root>\Sample\Python (without output\) to <Root>\deploy-backups\<stamp>,
  4. copies Sample\Python (keeping automation\config.json and output\), the operator
     folders (azure, gcp, deploy, icon) and, when changed, TTB.exe / NLog.config,
  5. installs requirements and runs unit tests + binary/doc checks, rolling back on failure,
  6. re-registers the scheduled tasks and starts TTB Automation if RunAs is logged on.
A first install (no <Root>\Sample\Python yet) skips the backup and cannot roll back.
#>
param(
    [Parameter(Mandatory)] [string]$Zip,
    [Parameter(Mandatory)] [string]$Sha256,
    [string]$Root = 'C:\TTB',
    [string]$Stamp = (Get-Date -Format 'yyyyMMdd-HHmmss'),
    [string]$RunAs = 'azureuser',
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$work = Join-Path $Root 'Sample\Python'
$incoming = Join-Path $Root 'deploy-incoming'
if ((Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash -ne $Sha256) { throw 'package SHA256 mismatch' }
$firstInstall = -not (Test-Path -LiteralPath (Join-Path $work 'automation'))

$statusDir = Join-Path $work 'output\supervisor'
New-Item -ItemType Directory -Path $statusDir -Force | Out-Null
$stopFile = Join-Path $statusDir 'stop.requested'
Set-Content -LiteralPath $stopFile -Value "deploy $Stamp" -Encoding ascii
$deadline = (Get-Date).AddSeconds(120)
do {
    $bots = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*sim_hft_bot.py*' })
    if ($bots.Count -eq 0) { break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)
if ($bots.Count -gt 0) {
    Remove-Item -LiteralPath $stopFile -Force
    throw 'bot did not stop within 120s; deployment aborted without changes'
}
Stop-ScheduledTask -TaskName 'TTB Automation' -ErrorAction SilentlyContinue

$backup = Join-Path $Root "deploy-backups\$Stamp"
if (-not $firstInstall) {
    robocopy $work $backup /E /XD output __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "backup failed (robocopy $LASTEXITCODE)" }
}
$staging = Join-Path $incoming $Stamp
Expand-Archive -LiteralPath $Zip -DestinationPath $staging -Force
robocopy (Join-Path $staging 'Sample\Python') $work /E /XF config.json /XD output /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copy failed (robocopy $LASTEXITCODE)" }
# Operator scripts, binary manifest, the README that check_docs_consistency reads,
# and icon (TTB.exe crashes at start without icon\TTBlogo.ico).
foreach ($folder in 'azure', 'gcp', 'deploy', 'icon') {
    $source = Join-Path $staging $folder
    if (-not (Test-Path -LiteralPath $source)) { continue }
    robocopy $source (Join-Path $Root $folder) /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "$folder copy failed (robocopy $LASTEXITCODE)" }
}
# TTB.exe is locked while TTB runs; stop it only when the binary actually changes.
# The supervisor relaunches and logs TTB back in once the task restarts.
foreach ($name in 'TTB.exe', 'NLog.config') {
    $source = Join-Path $staging $name
    $target = Join-Path $Root $name
    if (-not (Test-Path -LiteralPath $source)) { continue }
    if ((Test-Path -LiteralPath $target) -and
        (Get-FileHash -LiteralPath $source).Hash -eq (Get-FileHash -LiteralPath $target).Hash) { continue }
    if ($name -eq 'TTB.exe') {
        Get-Process -Name TTB -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
    Copy-Item -LiteralPath $source -Destination $target -Force
}

$failures = @()
# Windows PowerShell 5.1 turns native stderr into terminating errors under
# 'Stop'; rely on exit codes so a failed check still reaches the rollback.
$ErrorActionPreference = 'Continue'
Push-Location $work
try {
    $env:PYTHONUTF8 = '1'
    & py.exe -3.13 -m pip install -q --disable-pip-version-check -r requirements.txt 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { $failures += 'pip install' }
    # pywinauto imports win32ui, whose DLLs only resolve after pywin32's post-install step.
    & py.exe -3.13 -m pywin32_postinstall -install -quiet *> $null
    & py.exe -3.13 -m automation.check_binary_integrity --root $Root *> $null
    if ($LASTEXITCODE -ne 0) { $failures += 'binary integrity' }
    & py.exe -3.13 -m automation.check_docs_consistency *> $null
    if ($LASTEXITCODE -ne 0) { $failures += 'docs consistency' }
    if (-not $SkipTests) {
        & py.exe -3.13 -m unittest discover -q *> (Join-Path $statusDir "deploy-tests-$Stamp.log")
        if ($LASTEXITCODE -ne 0) { $failures += 'unit tests' }
    }
} finally {
    Pop-Location
}
$ErrorActionPreference = 'Stop'
if ($failures.Count -gt 0) {
    if (-not $firstInstall) {
        robocopy $backup $work /E /XD output /NFL /NDL /NJH /NJS /NP | Out-Null
    }
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    throw ("deployment failed" + $(if ($firstInstall) { ' (first install, nothing to roll back)' } else { ', rolled back' }) + ': ' + ($failures -join ', '))
}

Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $work 'automation\install_azure_tasks.ps1') -RunAsUser $RunAs -WorkDir $work | Out-Null
$loggedOn = [bool](Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue)
if ($loggedOn -and (Test-Path -LiteralPath (Join-Path $work 'automation\config.json'))) {
    Start-ScheduledTask -TaskName 'TTB Automation'
}
Remove-Item -LiteralPath $Zip -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
[ordered]@{
    deployed = $Stamp; backup = $(if ($firstInstall) { $null } else { $backup }); tests = (-not $SkipTests)
    configPresent = (Test-Path -LiteralPath (Join-Path $work 'automation\config.json'))
    userSession = $loggedOn
} | ConvertTo-Json -Compress
