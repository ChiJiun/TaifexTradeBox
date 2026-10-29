<#
.SYNOPSIS
Switch automation/config.json between shadow and live (ttb) with prerequisite checks.

.DESCRIPTION
Default is a dry run that prints the resulting config and check results.
With -Apply it backs up config.json, writes the new one and restarts the
TTB Automation supervisor. Switching to ttb requires every check to pass:
  * Credential Manager entry for auto-login exists (ttbCredentialTarget)
  * ttbCompetition is set
  * TTB API is logged in (all three queries 0000) and the TWD available
    margin is positive (ttb_login --check-only)
  * no manual-reconciliation flag
The live settings (risk arguments, auto-recovery) come from
config.competition.example.json; symbol, competition and credential target
are kept from the current config. -Template names another template (for
example config.grid-ccf.example.json); its symbol and strategy then win too.
The first live launch still runs the one-lot order/cancel probe (automation/live_gate.py) before any strategy order.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('shadow', 'ttb')][string]$Mode,
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$Template = 'config.competition.example.json',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$configPath = Join-Path $WorkDir 'automation\config.json'
$templatePath = Join-Path $WorkDir (Join-Path 'automation' $Template)
$current = Get-Content -Raw -LiteralPath $configPath -Encoding UTF8 | ConvertFrom-Json
$template = Get-Content -Raw -LiteralPath $templatePath -Encoding UTF8 | ConvertFrom-Json
$checks = [ordered]@{}

if ($Mode -eq 'ttb') {
    $new = $template
    $keepKeys = if ($Template -eq 'config.competition.example.json') {
        'symbol', 'ttbCompetition', 'ttbCredentialTarget', 'strategy', 'strategyModule'
    } else {
        'ttbCompetition', 'ttbCredentialTarget'
    }
    foreach ($keep in $keepKeys) {
        # An empty value (config.example.json ships strategyModule "") must not
        # replace the template's choice.
        if (-not [string]::IsNullOrWhiteSpace([string]$current.$keep)) { $new.$keep = $current.$keep }
    }
    # The supervisor confirms TTB chkAutoPut is off only through auto-login.
    $new.autoLogin = $true
    $target = if ($new.ttbCredentialTarget) { [string]$new.ttbCredentialTarget } else { 'TaifexTradeBox/TTB' }
    $checks.credentialStored = (((& cmdkey.exe "/list:$target" 2>&1) | Out-String) -match [regex]::Escape($target))
    $checks.competitionSet = -not [string]::IsNullOrWhiteSpace([string]$new.ttbCompetition)
    $checks.noReconcileFlag = -not (Test-Path (Join-Path $WorkDir 'output\supervisor\manual-reconciliation-required.flag'))
    Push-Location $WorkDir
    try {
        $env:PYTHONUTF8 = '1'
        $sessionJson = & py.exe -3.13 -m automation.ttb_login --check-only 2>$null | Out-String
        $session = if ($sessionJson) { ($sessionJson | ConvertFrom-Json).session } else { $null }
    } finally {
        Pop-Location
    }
    $checks.apiLoggedIn = [bool]($session -and $session.logged_in)
    $checks.twdMarginPositive = [bool]($session -and $session.twd_available_margin_positive)
    $checks.marginFieldPresent = [bool]($session -and $session.documented_margin_field_present)
} else {
    $new = $current
    $new.mode = 'shadow'
    $new.confirmVirtualOrders = $false
    $new.confirmFlatAccount = $false
    if ($new.autoRecovery) { $new.autoRecovery.enabled = $false }
}

$failed = @($checks.GetEnumerator() | Where-Object { -not $_.Value } | ForEach-Object { $_.Key })
[ordered]@{
    mode = $Mode
    apply = [bool]$Apply
    checks = $checks
    blocked = $failed
    config = $new
} | ConvertTo-Json -Depth 8

if ($failed.Count -gt 0) {
    Write-Error ("refusing {0}: failed checks {1}" -f $Mode, ($failed -join ', '))
    exit 2
}
if (-not $Apply) {
    Write-Host 'Dry run. Re-run with -Apply to write config.json and restart the supervisor.'
    exit 0
}

Copy-Item -LiteralPath $configPath -Destination ($configPath + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
[IO.File]::WriteAllText($configPath, ($new | ConvertTo-Json -Depth 8), (New-Object System.Text.UTF8Encoding($false)))
$stop = Join-Path $WorkDir 'output\supervisor\stop.requested'
Set-Content -LiteralPath $stop -Value "set_mode $Mode" -Encoding ascii
$deadline = (Get-Date).AddSeconds(120)
while ((Get-ScheduledTask -TaskName 'TTB Automation').State -eq 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep 3 }
Stop-ScheduledTask -TaskName 'TTB Automation' -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $stop -Force -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName 'TTB Automation'
Write-Host "config.json now mode=$Mode; TTB Automation restarted."
