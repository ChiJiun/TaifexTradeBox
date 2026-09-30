param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [int]$RetentionDays = 14
)

$ErrorActionPreference = 'Stop'
$cutoff = (Get-Date).AddDays(-[math]::Abs($RetentionDays))
$targets = @(
    (Join-Path $WorkDir 'output\runs'),
    (Join-Path $WorkDir 'output\order-probes')
)
$deleted = 0
$bytes = 0
foreach ($target in $targets) {
    if (-not (Test-Path -LiteralPath $target)) { continue }
    Get-ChildItem -LiteralPath $target -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        ForEach-Object {
            $bytes += $_.Length
            Remove-Item -LiteralPath $_.FullName -Force
            $deleted++
        }
}

[ordered]@{
    cleanedAt = (Get-Date).ToString('o')
    retentionDays = $RetentionDays
    filesDeleted = $deleted
    bytesDeleted = $bytes
} | ConvertTo-Json
