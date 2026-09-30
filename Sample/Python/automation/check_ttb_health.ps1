param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [int]$MaximumHeartbeatAgeSeconds = 30,
    [int]$StatusPort = 8765,
    [int]$MinimumFreeGb = 2
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
    statusHttp = $false
    readyHttp = $false
    reconcileFlag = Test-Path -LiteralPath (Join-Path $WorkDir 'output\supervisor\manual-reconciliation-required.flag')
    freeDiskGb = $null
}

try {
    $status = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/status" -f $StatusPort) -TimeoutSec 2
    $result.statusHttp = $true
} catch {}
try {
    $ready = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/readyz" -f $StatusPort) -TimeoutSec 2
    $result.readyHttp = [bool]$ready.ready
} catch {}
try {
    $root = [System.IO.Path]::GetPathRoot((Get-Item -LiteralPath $WorkDir).FullName).TrimEnd('\')
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$root'"
    if ($disk) { $result.freeDiskGb = [math]::Round($disk.FreeSpace / 1GB, 2) }
} catch {}

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
if ($result.reconcileFlag -or $result.botState -in @('error', 'manual_reconciliation_required')) {
    exit 3
}
if (-not $result.ttbProcess -or -not $result.port8080 -or -not $result.port51141 -or -not $result.heartbeatFresh -or -not $result.statusHttp -or -not $result.readyHttp) {
    exit 2
}
if ($null -ne $result.freeDiskGb -and $result.freeDiskGb -lt $MinimumFreeGb) {
    exit 4
}
exit 0
