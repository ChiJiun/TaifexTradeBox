<#
.SYNOPSIS
Deploy a committed Sample/Python snapshot to the Azure TTB VM through Azure VM Run Command.

.DESCRIPTION
Default is a dry run that only builds and hashes the package. With -Apply it:
  1. uploads a `git archive` of Sample/Python (+ azure/runtime_binaries.json) in base64 chunks,
  2. verifies the SHA256 on the VM,
  3. asks the running bot to stop gracefully (stop.requested) and stops the TTB Automation task,
  4. backs up C:\TTB\Sample\Python (without output\) to C:\TTB\deploy-backups\<stamp>,
  5. copies the new files while keeping automation\config.json and output\,
  6. installs requirements and runs unit tests + binary/doc checks, rolling back on failure,
  7. re-registers the scheduled tasks and starts TTB Automation if azureuser is logged on.
No GitHub credentials are stored on the VM. Only committed content is deployed.
#>
param(
    [string]$ResourceGroup = 'a112502533_rg_3957',
    [string]$VmName = 'ttb-sim-01',
    [string]$Ref = 'HEAD',
    [string]$RemoteRoot = 'C:\TTB',
    [string]$RunAsUser = 'azureuser',
    [int]$ChunkSize = 60000,
    [switch]$SkipTests,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$repo = (& git rev-parse --show-toplevel).Trim()
$commit = (& git -C $repo rev-parse --verify "$Ref^{commit}").Trim()
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$work = Join-Path $env:TEMP "ttb-deploy-$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null
$zip = Join-Path $work 'runtime.zip'
& git -C $repo archive --format=zip -o $zip $commit Sample/Python azure/runtime_binaries.json
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
$sha = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($zip))
$chunks = [math]::Ceiling($b64.Length / $ChunkSize)

$plan = [ordered]@{
    commit = $commit
    vm = "$ResourceGroup/$VmName"
    packageBytes = (Get-Item -LiteralPath $zip).Length
    sha256 = $sha
    uploadChunks = $chunks
    apply = [bool]$Apply
}
$plan | ConvertTo-Json
if (-not $Apply) {
    Write-Host 'Dry run only. Re-run with -Apply to upload and install on the VM.'
    exit 0
}

$dirty = (& git -C $repo status --porcelain -- Sample/Python azure | Out-String).Trim()
if ($dirty -and $Ref -eq 'HEAD') {
    Write-Warning 'Working tree has uncommitted changes; only the committed snapshot is deployed.'
}

function Invoke-VmScript([string]$Path) {
    $message = & az vm run-command invoke --resource-group $ResourceGroup --name $VmName `
        --command-id RunPowerShellScript --scripts "@$Path" --query 'value[].message' -o tsv
    if ($LASTEXITCODE -ne 0) { throw "az vm run-command failed for $Path" }
    return ($message | Out-String)
}

$incoming = "$RemoteRoot\deploy-incoming"
$remoteB64 = "$incoming\$stamp.b64"
for ($i = 0; $i -lt $chunks; $i++) {
    $part = $b64.Substring($i * $ChunkSize, [math]::Min($ChunkSize, $b64.Length - $i * $ChunkSize))
    $script = Join-Path $work "upload-$i.ps1"
    @"
New-Item -ItemType Directory -Path '$incoming' -Force | Out-Null
Add-Content -LiteralPath '$remoteB64' -Value '$part' -NoNewline -Encoding ascii
'chunk $($i + 1)/$chunks ok'
"@ | Set-Content -LiteralPath $script -Encoding ascii
    Write-Host (Invoke-VmScript $script).Trim()
}

$install = @'
$ErrorActionPreference = 'Stop'
$root = '__ROOT__'; $stamp = '__STAMP__'; $expected = '__SHA__'; $runAs = '__USER__'; $skipTests = __SKIPTESTS__
$work = Join-Path $root 'Sample\Python'
$incoming = Join-Path $root 'deploy-incoming'
$zip = Join-Path $incoming "$stamp.zip"
$text = (Get-Content -Raw -LiteralPath (Join-Path $incoming "$stamp.b64")).Trim()
[IO.File]::WriteAllBytes($zip, [Convert]::FromBase64String($text))
if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $expected) { throw 'package SHA256 mismatch' }

$statusDir = Join-Path $work 'output\supervisor'
New-Item -ItemType Directory -Path $statusDir -Force | Out-Null
$stopFile = Join-Path $statusDir 'stop.requested'
Set-Content -LiteralPath $stopFile -Value "deploy $stamp" -Encoding ascii
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

$backup = Join-Path $root "deploy-backups\$stamp"
robocopy $work $backup /E /XD output __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "backup failed (robocopy $LASTEXITCODE)" }
$staging = Join-Path $incoming $stamp
Expand-Archive -LiteralPath $zip -DestinationPath $staging -Force
robocopy (Join-Path $staging 'Sample\Python') $work /E /XF config.json /XD output /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copy failed (robocopy $LASTEXITCODE)" }
New-Item -ItemType Directory -Path (Join-Path $root 'azure') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $staging 'azure\runtime_binaries.json') -Destination (Join-Path $root 'azure\runtime_binaries.json') -Force

$failures = @()
Push-Location $work
try {
    $env:PYTHONUTF8 = '1'
    & py.exe -3.13 -m pip install -q --disable-pip-version-check -r requirements.txt 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { $failures += 'pip install' }
    & py.exe -3.13 -m automation.check_binary_integrity --root $root *> $null
    if ($LASTEXITCODE -ne 0) { $failures += 'binary integrity' }
    & py.exe -3.13 -m automation.check_docs_consistency *> $null
    if ($LASTEXITCODE -ne 0) { $failures += 'docs consistency' }
    if (-not $skipTests) {
        & py.exe -3.13 -m unittest discover -q *> (Join-Path $statusDir "deploy-tests-$stamp.log")
        if ($LASTEXITCODE -ne 0) { $failures += 'unit tests' }
    }
} finally {
    Pop-Location
}
if ($failures.Count -gt 0) {
    robocopy $backup $work /E /XD output /NFL /NDL /NJH /NJS /NP | Out-Null
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    throw ("deployment rolled back: " + ($failures -join ', '))
}

Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $work 'automation\install_azure_tasks.ps1') -RunAsUser $runAs -WorkDir $work | Out-Null
$loggedOn = [bool](Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue)
if ($loggedOn -and (Test-Path -LiteralPath (Join-Path $work 'automation\config.json'))) {
    Start-ScheduledTask -TaskName 'TTB Automation'
}
Remove-Item -LiteralPath (Join-Path $incoming "$stamp.b64"), $zip -Force -ErrorAction SilentlyContinue
[ordered]@{
    deployed = $stamp; backup = $backup; tests = (-not $skipTests)
    configPresent = (Test-Path -LiteralPath (Join-Path $work 'automation\config.json'))
    userSession = $loggedOn
} | ConvertTo-Json -Compress
'@
$install = $install.Replace('__ROOT__', $RemoteRoot).Replace('__STAMP__', $stamp).Replace('__SHA__', $sha)
$install = $install.Replace('__USER__', $RunAsUser).Replace('__SKIPTESTS__', $(if ($SkipTests) { '$true' } else { '$false' }))
$installPath = Join-Path $work 'install.ps1'
Set-Content -LiteralPath $installPath -Value $install -Encoding utf8
Write-Host 'Installing on VM (unit tests may take a few minutes)...'
Write-Host (Invoke-VmScript $installPath).Trim()
