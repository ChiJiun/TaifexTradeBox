param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$AlertWebhookUrl = $env:TTB_ALERT_WEBHOOK_URL,
    [int]$RepeatAlertMinutes = 60,
    # Taipei minutes of day: from 08:55 (the bot needs a few minutes after the
    # 08:45 open to log in and pass the live gate) to 13:40 (it flattens and
    # exits before the 13:45 close).
    [int]$AlertFromMinutes = 535,
    [int]$AlertUntilMinutes = 820
)

$ErrorActionPreference = 'Stop'
$healthScript = Join-Path $WorkDir 'automation\check_ttb_health.ps1'
$stateDir = Join-Path $WorkDir 'output\supervisor'
$statePath = Join-Path $stateDir 'health-watchdog-state.json'
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null

$output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $healthScript -WorkDir $WorkDir 2>&1 | Out-String
$exitCode = $LASTEXITCODE
$now = Get-Date
$previous = $null
if (Test-Path -LiteralPath $statePath) {
    try { $previous = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json } catch {}
}

# The bot only runs during the regular session, so a failing check outside it
# (VM just booted, bot already flattened after the close) is expected.
function Test-MarketHours([datetime]$At) {
    $local = [TimeZoneInfo]::ConvertTime($At, [TimeZoneInfo]::FindSystemTimeZoneById('Taipei Standard Time'))
    if ($local.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) { return $false }
    try {
        $calendar = Get-Content -Raw -LiteralPath (Join-Path $WorkDir 'automation\taifex_calendar_2026.json') | ConvertFrom-Json
        if ($calendar.closed_dates -contains $local.ToString('yyyy-MM-dd')) { return $false }
    } catch {}
    $minutes = $local.TimeOfDay.TotalMinutes
    return $minutes -ge $AlertFromMinutes -and $minutes -le $AlertUntilMinutes
}

$alerted = [bool]($previous -and $previous.alerted)
$lastAlertAt = if ($previous) { $previous.lastAlertAt } else { $null }
$shouldAlert = $false
$kind = $null
if ($exitCode -ne 0) {
    $kind = 'failure'
    if (Test-MarketHours $now) {
        $shouldAlert = -not $alerted -or -not $lastAlertAt -or
            ($now - [datetime]$lastAlertAt).TotalMinutes -ge $RepeatAlertMinutes
    }
} elseif ($alerted) {
    $kind = 'recovery'
    $shouldAlert = $true
}
if ($shouldAlert -and $AlertWebhookUrl) {
    $summary = 'TaifexTradeBox {0} on {1}: exit={2}' -f $kind, $env:COMPUTERNAME, $exitCode
    $detailText = $output.Trim()
    if ($detailText.Length -gt 1500) { $detailText = $detailText.Substring(0, 1500) }
    $payload = [ordered]@{
        # content (Discord) / text (Slack) let common chat webhooks render it.
        content = "$summary`n$detailText"
        text = "$summary`n$detailText"
        source = 'TaifexTradeBox'
        kind = $kind
        checkedAt = $now.ToString('o')
        exitCode = $exitCode
        host = $env:COMPUTERNAME
        details = $output.Trim()
    } | ConvertTo-Json -Depth 6
    try {
        Invoke-RestMethod -Method Post -Uri $AlertWebhookUrl -ContentType 'application/json' -Body $payload -TimeoutSec 10 | Out-Null
        $lastAlertAt = $now.ToString('o')
        $alerted = $kind -eq 'failure'
    } catch {
        Add-Content -LiteralPath (Join-Path $stateDir 'health-watchdog-error.log') -Value ("{0} webhook failed: {1}" -f $now.ToString('o'), $_.Exception.Message) -Encoding utf8
    }
}

[ordered]@{
    checkedAt = $now.ToString('o')
    exitCode = $exitCode
    kind = $kind
    lastAlertAt = $lastAlertAt
    alerted = $alerted
} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8

Write-Output $output.Trim()
exit $exitCode
