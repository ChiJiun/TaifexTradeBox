<#
.SYNOPSIS
Point the GCP RDP/SSH firewall rule at your current public IPv4 (/32).

.DESCRIPTION
Default is a dry run that prints the current and proposed source range. -Apply
updates the rule. Never widens the rule beyond a single public /32.
#>
param(
    [switch]$Apply,
    [string]$Project = 'ttb-competition',
    [string]$RuleName = 'ttb-allow-rdp-ssh-home',
    [string]$IpAddress = ''
)

$ErrorActionPreference = 'Stop'
if (-not $IpAddress) {
    $IpAddress = ([string](Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 10)).Trim()
}
$parsed = $null
if (-not [System.Net.IPAddress]::TryParse($IpAddress, [ref]$parsed) -or
    $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    $IpAddress -notmatch '^(?:\d{1,3}\.){3}\d{1,3}$') {
    throw "Invalid public IPv4 address: $IpAddress"
}
if ($IpAddress -match '^(?:10\.|127\.|0\.|169\.254\.|192\.168\.|255\.)' -or
    $IpAddress -match '^172\.(?:1[6-9]|2[0-9]|3[01])\.' -or
    $IpAddress -match '^(?:22[4-9]|23[0-9])\.') {
    throw "Refusing to whitelist private, loopback or multicast address: $IpAddress"
}

$current = (& gcloud compute firewall-rules describe $RuleName --project $Project --format 'value(sourceRanges)').Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to read firewall rule $RuleName" }
[ordered]@{ rule = $RuleName; current = $current; proposed = "$IpAddress/32"; apply = [bool]$Apply } | ConvertTo-Json
if (-not $Apply) { Write-Host 'Dry run only. Re-run with -Apply to update the rule.'; exit 0 }
& gcloud compute firewall-rules update $RuleName --project $Project --source-ranges "$IpAddress/32" --quiet
if ($LASTEXITCODE -ne 0) { throw 'firewall update failed' }
