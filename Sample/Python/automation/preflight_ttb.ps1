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

# Fail closed on an unexpected TTB.exe/TTBHelp.pyc pair. A matching hash
# proves only equivalence to the reviewed repository snapshot, NOT official
# publisher provenance or a safe TTB GUI session.
if (Test-Path -LiteralPath $WorkDir) {
    $repoRoot = [System.IO.Path]::GetFullPath((Join-Path $WorkDir '..\..'))
    try {
        Push-Location $WorkDir
        & py.exe -3.13 -m automation.check_binary_integrity --root $repoRoot *> $null
        if ($LASTEXITCODE -ne 0) {
            Add-Failure 'TTB binary SHA256/magic mismatch or missing manifest; refuse startup'
        } else {
            Add-Warning 'TTB snapshot hashes match but official provenance/compatibility is NOT authenticated'
        }
    } catch {
        Add-Failure "TTB binary integrity validation failed: $($_.Exception.Message)"
    } finally {
        Pop-Location
    }
}

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
        if ($config.autoLogin) {
            $target = if ($config.ttbCredentialTarget) { [string]$config.ttbCredentialTarget } else { 'TaifexTradeBox/TTB' }
            $listing = (& cmdkey.exe "/list:$target" 2>&1 | Out-String)
            if ($listing -notmatch [regex]::Escape($target)) {
                Add-Failure "autoLogin requires a Credential Manager entry: cmdkey /generic:$target /user:<email> /pass"
            }
            if ([string]::IsNullOrWhiteSpace([string]$config.ttbCompetition)) {
                Add-Warning 'ttbCompetition is empty; login only succeeds when exactly one competition is listed'
            }
        }
        if ($config.autoRecovery -and $config.autoRecovery.enabled -and -not $config.autoRecovery.exclusiveBotAccount) {
            Add-Failure 'autoRecovery.enabled requires exclusiveBotAccount=true (nobody trades this account manually)'
        }
        if ($config.mode -eq 'ttb') {
            $extras = @($config.extraArgs | ForEach-Object { [string]$_ })
            foreach ($name in @('--max-notional', '--min-available-margin')) {
                $index = [array]::IndexOf($extras, $name)
                $value = 0.0
                if ($index -lt 0 -or ($index + 1) -ge $extras.Count -or
                    -not [double]::TryParse($extras[$index + 1], [ref]$value) -or $value -le 0) {
                    Add-Failure "ttb mode requires a positive $name in extraArgs"
                }
            }
            $marginFieldIndex = [array]::IndexOf($extras, '--available-margin-field')
            if ($marginFieldIndex -lt 0 -or ($marginFieldIndex + 1) -ge $extras.Count -or
                [string]::IsNullOrWhiteSpace($extras[$marginFieldIndex + 1])) {
                Add-Failure 'ttb mode requires a verified --available-margin-field in extraArgs'
            }
            $calendarIndex = [array]::IndexOf($extras, '--calendar-file')
            $calendarName = 'automation\taifex_calendar_2026.json'
            if ($calendarIndex -ge 0) {
                if (($calendarIndex + 1) -ge $extras.Count) {
                    Add-Failure 'ttb mode --calendar-file is missing its argument'
                } else {
                    $calendarName = $extras[$calendarIndex + 1]
                }
            }
            $calendarPath = if ([System.IO.Path]::IsPathRooted($calendarName)) {
                $calendarName
            } else {
                Join-Path $WorkDir $calendarName
            }
            if (-not (Test-Path -LiteralPath $calendarPath)) {
                Add-Failure "verified TAIFEX calendar missing: $calendarPath"
            } else {
                try {
                    $calendar = Get-Content -Raw -LiteralPath $calendarPath | ConvertFrom-Json
                    $today = (Get-Date).Date
                    $start = [datetime]::ParseExact([string]$calendar.starts, 'yyyy-MM-dd', $null)
                    $end = [datetime]::ParseExact([string]$calendar.ends, 'yyyy-MM-dd', $null)
                    if ($today -lt $start -or $today -gt $end) {
                        Add-Failure "TAIFEX calendar has no verified coverage for $($today.ToString('yyyy-MM-dd'))"
                    }
                } catch {
                    Add-Failure "TAIFEX calendar is invalid: $($_.Exception.Message)"
                }
            }
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
