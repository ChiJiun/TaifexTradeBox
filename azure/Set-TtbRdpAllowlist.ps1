param(
    [switch]$Apply,
    [string]$ResourceGroup = 'a112502533_rg_3957',
    [string]$NsgName = 'ttb-sim-01-nsg',
    [string]$RuleName = 'Allow-RDP-From-Home',
    [string]$IpAddress = ''
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw 'Azure CLI not found; install it and run az login'
}
if (-not $IpAddress) {
    $IpAddress = ([string](Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 10)).Trim()
}
$parsed = $null
if (-not [System.Net.IPAddress]::TryParse($IpAddress, [ref]$parsed) -or
    $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    $IpAddress -notmatch '^(?:\d{1,3}\.){3}\d{1,3}$') {
    throw "Invalid public IPv4 address: $IpAddress"
}
$privateOrSpecial = (
    $IpAddress -match '^(?:10\.|127\.|0\.|169\.254\.|192\.168\.|255\.)' -or
    $IpAddress -match '^172\.(?:1[6-9]|2[0-9]|3[01])\.' -or
    $IpAddress -match '^(?:22[4-9]|23[0-9])\.'
)
if ($privateOrSpecial) {
    throw "Refusing to whitelist private, loopback or multicast address: $IpAddress"
}
$ipRange = "$IpAddress/32"

$json = & az network nsg rule show --resource-group $ResourceGroup --nsg-name $NsgName --name $RuleName --output json
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to retrieve current RDP network rule; Azure sign-in/permission may be missing'
}
$rule = ($json | Out-String | ConvertFrom-Json)
if ($rule.access -ne 'Allow' -or $rule.direction -ne 'Inbound' -or
    $rule.protocol -notin @('Tcp', '*') -or [string]$rule.destinationPortRange -ne '3389') {
    throw 'The selected NSG rule is not the expected allow-inbound TCP/3389 rule'
}

$before = @($rule.sourceAddressPrefixes)
if ($rule.sourceAddressPrefix) { $before += [string]$rule.sourceAddressPrefix }
if ($Apply) {
    # Azure CLI account access remains available even if a user's RDP IP changes.
    & az network nsg rule update --resource-group $ResourceGroup --nsg-name $NsgName --name $RuleName --source-address-prefixes $ipRange --output none
    if ($LASTEXITCODE -ne 0) {
        throw 'Azure NSG update failed'
    }

    $afterRaw = & az network nsg rule show --resource-group $ResourceGroup --nsg-name $NsgName --name $RuleName --output json
    if ($LASTEXITCODE -ne 0) { throw 'Unable to verify Azure NSG change' }
    $afterRule = ($afterRaw | Out-String | ConvertFrom-Json)
    $after = @($afterRule.sourceAddressPrefixes)
    if ($afterRule.sourceAddressPrefix) { $after += [string]$afterRule.sourceAddressPrefix }
    if ($after.Count -ne 1 -or $after[0] -ne $ipRange) {
        throw "Azure returned unexpected RDP allowlist: $($after -join ', ')"
    }
} else {
    $after = $before
}
[ordered]@{
    resourceGroup = $ResourceGroup
    networkSecurityGroup = $NsgName
    rule = $RuleName
    before = $before
    currentPublicIpv4 = $IpAddress
    proposedCidr = $ipRange
    applied = [bool]$Apply
    after = $after
} | ConvertTo-Json -Depth 5

if (-not $Apply) {
    Write-Warning 'Audit only. Add -Apply to restrict RDP 3389 to your current IPv4 /32.'
}
