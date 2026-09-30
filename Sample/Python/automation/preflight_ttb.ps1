param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$ConfigPath = 'C:\TTB\Sample\Python\automation\config.json',
    [string]$TtbExe = 'C:\TTB\TTB.exe',
    [int]$MinimumFreeGb = 5
)

$ErrorActionPreference = 'Stop'
$failures = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

function Add-Failure([string]$Message) { $failures.Add($Message) }
function Add-Warning([string]$Message) { $warnings.Add($Message) }

if (-not (Test-Path -LiteralPath $WorkDir)) { Add-Failure "WorkDir missing: $WorkDir" }
if (-not (Test-Path -LiteralPath $ConfigPath)) { Add-Failure "Config missing: $ConfigPath" }
if (-not (Test-Path -LiteralPath $TtbExe)) { Add-Failure "TTB executable missing: $TtbExe" }

$python = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $python) {
    Add-Failure 'py.exe not found'
} else {
    try {
        $version = (& py.exe -3.13 --version 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { Add-Failure 'Python 3.13 launcher target unavailable' }
    } catch {
        Add-Failure "Python 3.13 check failed: $($_.Exception.Message)"
    }
}

$config = $null
if (Test-Path -LiteralPath $ConfigPath) {
    try {
        $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
        if (-not $config.symbol) { Add-Failure 'config.symbol is required' }
        if ($config.mode -notin @('shadow', 'ttb')) { Add-Failure 'config.mode must be shadow or ttb' }
        if ($config.mode -eq 'ttb' -and (-not $config.confirmVirtualOrders -or -not $config.confirmFlatAccount)) {
            Add-Failure 'ttb mode requires both confirmation flags'
        }
    } catch {
        Add-Failure "Config JSON invalid: $($_.Exception.Message)"
    }
}

try {
    $drive = Get-Item -LiteralPath $WorkDir
    $root = [System.IO.Path]::GetPathRoot($drive.FullName)
    $deviceId = $root.TrimEnd('\')
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$deviceId'"
    if ($disk) {
        $freeGb = [math]::Round($disk.FreeSpace / 1GB, 2)
        if ($freeGb -lt $MinimumFreeGb) { Add-Failure ("Free disk space too low: {0}GB" -f $freeGb) }
        elseif ($freeGb -lt ($MinimumFreeGb * 2)) { Add-Warning ("Free disk space is only {0}GB" -f $freeGb) }
    }
} catch {
    Add-Warning "Disk check failed: $($_.Exception.Message)"
}

$reconcileFlag = Join-Path $WorkDir 'output\supervisor\manual-reconciliation-required.flag'
if (Test-Path -LiteralPath $reconcileFlag) {
    Add-Failure "Manual reconciliation flag exists: $reconcileFlag"
}

$testResult = $null
if ((Test-Path -LiteralPath $WorkDir) -and $python) {
    Push-Location $WorkDir
    try {
        & py.exe -3.13 -m unittest discover -q
        $testResult = $LASTEXITCODE
        if ($testResult -ne 0) { Add-Failure "Unit tests failed with exit code $testResult" }
    } catch {
        Add-Failure "Unit tests could not run: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }
}

$result = [ordered]@{
    checkedAt = (Get-Date).ToString('o')
    workDir = $WorkDir
    configPath = $ConfigPath
    ttbExe = $TtbExe
    mode = if ($config) { $config.mode } else { $null }
    symbol = if ($config) { $config.symbol } else { $null }
    unitTestExitCode = $testResult
    failures = @($failures)
    warnings = @($warnings)
    ok = ($failures.Count -eq 0)
}
$result | ConvertTo-Json -Depth 6
if ($failures.Count -gt 0) { exit 2 }
exit 0
