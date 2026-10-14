param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [int]$MaxHeartbeatAgeSeconds = 60
)

$ErrorActionPreference = 'Stop'
if ($MaxHeartbeatAgeSeconds -lt 5) {
    throw 'MaxHeartbeatAgeSeconds must be >=5'
}
$healthFile = Join-Path $WorkDir 'output\health.json'
$reconciliation = Join-Path $WorkDir 'output\supervisor\manual-reconciliation-required.flag'

function Test-Listener([int]$Port) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}
function Task-Summary([string]$Name) {
    try {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction Stop
        return [ordered]@{
            installed = $true
            state = [string]$task.State
            lastTaskResult = [int64]$info.LastTaskResult
        }
    } catch {
        return [ordered]@{ installed = $false; state = 'missing'; lastTaskResult = $null }
    }
}

$tasks = [ordered]@{
    gui = Task-Summary 'TTB GUI'
    automation = Task-Summary 'TTB Automation'
    watchdog = Task-Summary 'TTB Health Watchdog'
}
$health = [ordered]@{
    exists = Test-Path -LiteralPath $healthFile
    readable = $false
    symbol = $null
    mode = $null
    state = $null
    heartbeatFresh = $false
    acceptedQuoteFresh = $false
    acceptedQuoteAgeSeconds = $null
    positionKnown = $false
    positionOpen = $false
    ageSeconds = $null
}
if ($health.exists) {
    try {
        $row = Get-Content -Raw -LiteralPath $healthFile | ConvertFrom-Json
        $age = ([datetime]::UtcNow - ([datetime]$row.updated_at).ToUniversalTime()).TotalSeconds
        $health.readable = $true
        $health.symbol = [string]$row.symbol
        $health.mode = [string]$row.mode
        $health.state = [string]$row.state
        $health.ageSeconds = [math]::Round($age, 2)
        $health.heartbeatFresh = ($age -ge -10 -and $age -le $MaxHeartbeatAgeSeconds)
        $health.positionKnown = $null -ne $row.position
        if ($health.positionKnown) {
            $health.positionOpen = ([double]$row.position -ne 0)
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$row.last_quote_wall_utc)) {
            $last = [datetimeoffset]::Parse([string]$row.last_quote_wall_utc)
            $quoteAge = ([datetimeoffset]::UtcNow - $last.ToUniversalTime()).TotalSeconds
            $health.acceptedQuoteAgeSeconds = [math]::Round($quoteAge, 2)
            $health.acceptedQuoteFresh = ($quoteAge -ge -10 -and $quoteAge -le $MaxHeartbeatAgeSeconds)
        }
    } catch {
        $health.readable = $false
    }
}

$binaryRoot = Split-Path -Parent (Split-Path -Parent $WorkDir)
$manifest = Join-Path $binaryRoot 'azure\runtime_binaries.json'
$required = [ordered]@{
    captureCli = Test-Path -LiteralPath (Join-Path $WorkDir 'ttb_readonly_capture.py')
    reconcileCli = Test-Path -LiteralPath (Join-Path $WorkDir 'reconcile_ttb.py')
    releaseValidator = Test-Path -LiteralPath (Join-Path $WorkDir 'automation\validate_release.ps1')
    healthWatchdogScript = Test-Path -LiteralPath (Join-Path $WorkDir 'automation\health_watchdog.ps1')
    operatorRunbook = Test-Path -LiteralPath (Join-Path $WorkDir 'OPERATOR_RUNBOOK.md')
    binaryManifest = Test-Path -LiteralPath $manifest
}
# A folder can contain old runtime code even after newer helper scripts
# have been copied into place. Flag missing runtime *capabilities* separately.
# This is a deployment-drift heuristic, not cryptographic provenance.
$runtimeFeatures = [ordered]@{
    staleFatalGuard = $false
    exchangeLagGuard = $false
    quoteSequenceIntegrity = $false
    acceptedQuoteWallTime = $false
}
try {
    $bot = Get-Content -Raw -LiteralPath (Join-Path $WorkDir 'sim_hft_bot.py') -ErrorAction Stop
    $engine = Get-Content -Raw -LiteralPath (Join-Path $WorkDir 'ttb\engine.py') -ErrorAction Stop
    $feed = Get-Content -Raw -LiteralPath (Join-Path $WorkDir 'ttb\market_data.py') -ErrorAction Stop
    $runtimeFeatures.staleFatalGuard = (
        $bot.Contains('--stale-fatal-seconds') -and $engine.Contains('stale_fatal_seconds')
    )
    $runtimeFeatures.exchangeLagGuard = (
        $bot.Contains('--max-exchange-lag-seconds') -and $feed.Contains('max_exchange_lag_seconds')
    )
    $runtimeFeatures.quoteSequenceIntegrity = $feed.Contains('class MarketDataIntegrity')
    $runtimeFeatures.acceptedQuoteWallTime = $engine.Contains('last_quote_wall_utc')
} catch {
    # Missing or unreadable core source code is unsafe: leave all flags false.
}
$processRunning = [bool](Get-Process -Name TTB -ErrorAction SilentlyContinue)
$apiListening = Test-Listener 8080
$zmqListening = Test-Listener 51141
$flagPresent = Test-Path -LiteralPath $reconciliation
$healthyState = $health.state -in @('healthy', 'waiting_market')
$issues = [System.Collections.Generic.List[string]]::new()
if (-not $processRunning) { $issues.Add('ttb_gui_process_not_running') }
if (-not $apiListening) { $issues.Add('api_port_8080_missing') }
if (-not $zmqListening) { $issues.Add('quote_port_51141_missing') }
if (-not $tasks.automation.installed) { $issues.Add('automation_task_missing') }
if (-not $tasks.watchdog.installed) { $issues.Add('watchdog_task_missing') }
if (-not $health.readable) { $issues.Add('health_missing_or_unreadable') }
elseif (-not $health.heartbeatFresh) { $issues.Add('health_heartbeat_stale') }
if ($health.readable -and -not $healthyState) { $issues.Add('unhealthy_bot_state') }
if ($health.readable -and -not $health.positionKnown) { $issues.Add('position_unknown') }
if ($health.readable -and $health.state -eq 'healthy' -and -not $health.acceptedQuoteFresh) {
    $issues.Add('accepted_quote_missing_or_stale')
}
if ($health.readable -and $health.state -eq 'waiting_market' -and $health.positionOpen -and -not $health.acceptedQuoteFresh) {
    $issues.Add('unvalued_position_during_market_close')
}
if ($flagPresent) { $issues.Add('manual_reconciliation_required') }
foreach ($key in $required.Keys) {
    if (-not $required[$key]) { $issues.Add('missing_' + $key) }
}
foreach ($key in $runtimeFeatures.Keys) {
    if (-not $runtimeFeatures[$key]) { $issues.Add('missing_runtime_feature_' + $key) }
}
$result = [ordered]@{
    schema = 'ttb-vm-readiness-v1'
    checkedAt = (Get-Date).ToUniversalTime().ToString('o')
    safeForSupervisedShadowSoak = ($issues.Count -eq 0)
    tradingReady = ($issues.Count -eq 0 -and $health.state -eq 'healthy' -and $health.acceptedQuoteFresh)
    supervisedLiveTradingApproved = $false
    ttbProcess = $processRunning
    api8080 = $apiListening
    quote51141 = $zmqListening
    tasks = $tasks
    health = $health
    manualReconciliationFlag = $flagPresent
    requiredFiles = $required
    runtimeSafetyFeatures = $runtimeFeatures
    blockers = @($issues)
    note = 'Read-only VM audit. Does not authenticate TTB GUI account, true broker margin or API fills.'
}
$result | ConvertTo-Json -Depth 8
if ($issues.Count -gt 0) { exit 2 }
exit 0
