param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [int]$MaximumHeartbeatAgeSeconds = 30
)

$healthPath = Join-Path $WorkDir 'output\health.json'
$result = [ordered]@{
    checkedAt = (Get-Date).ToString('o')
    ttbProcess = [bool](Get-Process -Name TTB -ErrorAction SilentlyContinue)
    pythonProcess = [bool](Get-Process -Name python -ErrorAction SilentlyContinue)
    port8080 = [bool](Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue)
    port51141 = [bool](Get-NetTCPConnection -State Listen -LocalPort 51141 -ErrorAction SilentlyContinue)
    healthFile = Test-Path -LiteralPath $healthPath
    heartbeatFresh = $false
    botState = $null
    symbol = $null
    mode = $null
    position = $null
}

if ($result.healthFile) {
    $health = Get-Content -Raw -LiteralPath $healthPath | ConvertFrom-Json
    $age = ((Get-Date).ToUniversalTime() - ([datetime]$health.updated_at).ToUniversalTime()).TotalSeconds
    $result.heartbeatFresh = $age -le $MaximumHeartbeatAgeSeconds
    $result.botState = $health.state
    $result.symbol = $health.symbol
    $result.mode = $health.mode
    $result.position = $health.position
    $result.heartbeatAgeSeconds = [math]::Round($age, 1)
}

$result | ConvertTo-Json -Depth 6
if (-not $result.ttbProcess -or -not $result.port8080 -or -not $result.port51141 -or -not $result.heartbeatFresh) {
    exit 2
}
if ($result.botState -in @('error', 'manual_reconciliation_required')) {
    exit 3
}
exit 0
