param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$AlertWebhookUrl = $env:TTB_ALERT_WEBHOOK_URL,
    [int]$RepeatAlertMinutes = 60
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

$shouldAlert = $false
$kind = $null
if ($exitCode -ne 0) {
    $kind = 'failure'
    if (-not $previous -or [int]$previous.exitCode -eq 0) {
        $shouldAlert = $true
    } elseif ($previous.lastAlertAt) {
        $lastAlert = [datetime]$previous.lastAlertAt
        if (($now - $lastAlert).TotalMinutes -ge $RepeatAlertMinutes) { $shouldAlert = $true }
    } else {
        $shouldAlert = $true
    }
} elseif ($previous -and [int]$previous.exitCode -ne 0) {
    $kind = 'recovery'
    $shouldAlert = $true
}

$lastAlertAt = if ($previous) { $previous.lastAlertAt } else { $null }
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
    } catch {
        Add-Content -LiteralPath (Join-Path $stateDir 'health-watchdog-error.log') -Value ("{0} webhook failed: {1}" -f $now.ToString('o'), $_.Exception.Message) -Encoding utf8
    }
}

[ordered]@{
    checkedAt = $now.ToString('o')
    exitCode = $exitCode
    kind = $kind
    lastAlertAt = $lastAlertAt
} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8

Write-Output $output.Trim()
exit $exitCode
