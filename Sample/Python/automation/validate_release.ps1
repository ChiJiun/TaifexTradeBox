param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [switch]$SkipUnitTests
)

$ErrorActionPreference = 'Stop'
$checks = @()

function Add-Check([string]$Name, [bool]$Ok, [string]$Details = '') {
    $script:checks += [pscustomobject][ordered]@{
        name = $Name
        ok = $Ok
        details = $Details
    }
}

$requiredFiles = @(
    'sim_hft_bot.py',
    'reconcile_ttb.py',
    'OPERATOR_RUNBOOK.md',
    'requirements.txt',
    'ttb\api.py',
    'ttb\engine.py',
    'ttb\market_data.py',
    'ttb\orders.py',
    'ttb\reconciliation.py',
    'ttb\contracts.py',
    'ttb\journal.py',
    'ttb\risk.py',
    'ttb\runtime.py',
    'ttb\status_server.py',
    'automation\config.example.json',
    'automation\run_ttb_automation.ps1',
    'automation\check_ttb_health.ps1',
    'automation\health_watchdog.ps1',
    'automation\preflight_ttb.ps1',
    'automation\cleanup_runtime.ps1',
    'automation\install_azure_tasks.ps1'
)

if (-not (Test-Path -LiteralPath $WorkDir)) {
    Add-Check 'workdir' $false "missing: $WorkDir"
    $checks | ConvertTo-Json -Depth 6
    exit 2
}

$missing = @(
    $requiredFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $WorkDir $_))
    }
)
$requiredDetails = 'all present'
if ($missing.Count -gt 0) {
    $requiredDetails = 'missing: ' + ($missing -join ', ')
}
Add-Check 'required-files' ($missing.Count -eq 0) $requiredDetails

$pythonOk = $false
$pythonDetails = ''
try {
    $pythonDetails = (& py.exe -3.13 --version 2>&1 | Out-String).Trim()
    $pythonOk = ($LASTEXITCODE -eq 0)
} catch {
    $pythonDetails = $_.Exception.Message
}
Add-Check 'python-3.13' $pythonOk $pythonDetails

Push-Location $WorkDir
try {
    if ($pythonOk) {
        & py.exe -3.13 -m compileall -q .
        Add-Check 'python-compileall' ($LASTEXITCODE -eq 0) "exit=$LASTEXITCODE"

        & py.exe -3.13 sim_hft_bot.py --help *> $null
        Add-Check 'cli-help' ($LASTEXITCODE -eq 0) "exit=$LASTEXITCODE"

        & py.exe -3.13 reconcile_ttb.py --help *> $null
        Add-Check 'reconcile-cli-help' ($LASTEXITCODE -eq 0) "exit=$LASTEXITCODE"

        if (-not $SkipUnitTests) {
            & py.exe -3.13 -m unittest discover -q
            Add-Check 'unit-tests' ($LASTEXITCODE -eq 0) "exit=$LASTEXITCODE"
        }
    }
} finally {
    Pop-Location
}

$configOk = $false
$configDetails = ''
try {
    $config = Get-Content -Raw -LiteralPath (Join-Path $WorkDir 'automation\config.example.json') | ConvertFrom-Json
    $configOk = (
        [bool]$config.symbol -and
        $config.mode -in @('shadow', 'ttb') -and
        [bool]$config.strategy
    )
    $configDetails = "symbol=$($config.symbol) mode=$($config.mode) strategy=$($config.strategy)"
} catch {
    $configDetails = $_.Exception.Message
}
Add-Check 'example-config' $configOk $configDetails

$failed = @($checks | Where-Object { -not $_.ok })
[ordered]@{
    checkedAt = (Get-Date).ToString('o')
    workDir = $WorkDir
    ok = ($failed.Count -eq 0)
    checks = $checks
} | ConvertTo-Json -Depth 8

if ($failed.Count -gt 0) { exit 2 }
exit 0
