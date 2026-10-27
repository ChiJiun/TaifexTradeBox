<#
.SYNOPSIS
Deploy a committed Sample/Python snapshot to the Azure TTB VM through Azure VM Run Command.

.DESCRIPTION
Default is a dry run that only builds and hashes the package. With -Apply it
uploads a `git archive` of Sample/Python, azure and deploy in base64 chunks and runs
deploy/Install-TtbRuntime.ps1 on the VM (verify SHA256, stop the bot gracefully,
back up, copy, test, roll back on failure, restart TTB Automation).
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
& git -C $repo archive --format=zip -o $zip $commit Sample/Python azure deploy
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

# The VM-side steps live in deploy/Install-TtbRuntime.ps1 (shared with gcp/);
# Run Command takes one script, so decode the package and invoke it inline.
$installBody = (& git -C $repo show "${commit}:deploy/Install-TtbRuntime.ps1") -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'deploy/Install-TtbRuntime.ps1 is not in the deployed commit' }
$skip = if ($SkipTests) { ' -SkipTests' } else { '' }
$install = @"
`$ErrorActionPreference = 'Stop'
`$zip = '$incoming\$stamp.zip'
`$text = (Get-Content -Raw -LiteralPath '$remoteB64').Trim()
[IO.File]::WriteAllBytes(`$zip, [Convert]::FromBase64String(`$text))
Remove-Item -LiteralPath '$remoteB64' -Force
& {
$installBody
} -Zip `$zip -Sha256 '$sha' -Root '$RemoteRoot' -Stamp '$stamp' -RunAs '$RunAsUser'$skip
"@
$installPath = Join-Path $work 'install.ps1'
Set-Content -LiteralPath $installPath -Value $install -Encoding utf8
Write-Host 'Installing on VM (unit tests may take a few minutes)...'
Write-Host (Invoke-VmScript $installPath).Trim()
