<#
.SYNOPSIS
GCE Windows shutdown script: ask the trading bot to stop before the VM powers off.

.DESCRIPTION
Installed as instance metadata `windows-shutdown-script-ps1` by
gcp/Set-TtbInstanceSchedule.ps1, so it runs on every stop (scheduled or manual).
It writes bot-stop.requested (never stop.requested: that file would survive the
reboot and make the supervisor refuse live startup) and waits up to 60 seconds
for sim_hft_bot.py to exit. The engine already exits positions from 10 minutes
before the close, and the next startup checks the account is flat, so this is a
best-effort graceful stop, not the only safeguard.
#>
$statusDir = 'C:\TTB\Sample\Python\output\supervisor'
$log = Join-Path $statusDir 'supervisor.log'
function Write-Log([string]$Message) {
    Add-Content -LiteralPath $log -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) -Encoding utf8
}

$bots = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*sim_hft_bot.py*' })
if ($bots.Count -eq 0) {
    Write-Log 'VM shutdown: no bot running'
    exit 0
}
Set-Content -LiteralPath (Join-Path $statusDir 'bot-stop.requested') -Value "vm-shutdown $(Get-Date -Format o)" -Encoding ascii
Write-Log 'VM shutdown: asking bot to stop'
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $bots = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*sim_hft_bot.py*' })
    if ($bots.Count -eq 0) {
        Write-Log 'VM shutdown: bot stopped'
        exit 0
    }
}
Write-Log 'VM shutdown: bot still running after 60s; the next startup checks the account is flat'
