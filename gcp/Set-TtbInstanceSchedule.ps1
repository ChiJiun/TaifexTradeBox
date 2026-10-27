<#
.SYNOPSIS
Start the GCP TTB VM before the regular session and stop it after the close.

.DESCRIPTION
Default is a dry run that prints the current state and the planned changes.
-Apply (idempotent):
  1. grants the Compute Engine service agent roles/compute.instanceAdmin.v1
     (an instance schedule cannot start/stop the VM without it),
  2. creates the instance-schedule resource policy (weekdays, Asia/Taipei),
  3. attaches it to the VM,
  4. installs gcp/shutdown-stop-bot.ps1 as the `windows-shutdown-script-ps1`
     metadata so every stop asks the bot to stop first.
-Remove detaches the policy and the shutdown script; the VM then runs 24/7 again.
Taiwan public holidays are not excluded: the VM boots, the bot finds no
session and nothing trades. Night-session trading is incompatible with this.
#>
param(
    [string]$Project = 'ttb-competition',
    [string]$Zone = 'asia-east1-b',
    [string]$Instance = 'ttb-sim-gcp',
    [string]$PolicyName = 'ttb-market-hours',
    [string]$StartCron = '0 8 * * 1-5',
    [string]$StopCron = '0 14 * * 1-5',
    [string]$TimeZone = 'Asia/Taipei',
    [switch]$Remove,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$region = $Zone.Substring(0, $Zone.LastIndexOf('-'))
$shutdownScript = Join-Path $PSScriptRoot 'shutdown-stop-bot.ps1'
$gc = @('--project', $Project, '--quiet')

# Windows PowerShell 5.1 turns gcloud's stderr into terminating errors under
# 'Stop'; read exit codes instead.
function Invoke-Gcloud([string[]]$Arguments, [switch]$AllowFailure) {
    $ErrorActionPreference = 'Continue'
    $output = (& gcloud @Arguments @gc 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) { throw "gcloud $($Arguments -join ' ') failed: $output" }
    return [pscustomobject]@{ ok = ($LASTEXITCODE -eq 0); output = $output }
}

$projectNumber = (Invoke-Gcloud @('projects', 'describe', $Project, '--format', 'value(projectNumber)')).output
$agent = "serviceAccount:service-$projectNumber@compute-system.iam.gserviceaccount.com"
$policy = Invoke-Gcloud @('compute', 'resource-policies', 'describe', $PolicyName, '--region', $region,
    '--format', 'json(instanceSchedulePolicy)') -AllowFailure
$attached = (Invoke-Gcloud @('compute', 'instances', 'describe', $Instance, '--zone', $Zone,
    '--format', 'value(resourcePolicies)')).output -match "/$PolicyName$|/$PolicyName;"
$hasShutdownScript = (Invoke-Gcloud @('compute', 'instances', 'describe', $Instance, '--zone', $Zone,
    '--format', 'value(metadata.items[].key)')).output -match 'windows-shutdown-script-ps1'

[ordered]@{
    instance = "$Project/$Zone/$Instance"
    policy = "$region/$PolicyName"
    policyExists = $policy.ok
    attached = $attached
    shutdownScriptInstalled = $hasShutdownScript
    planned = $(if ($Remove) { 'detach policy and shutdown script' } else { "start '$StartCron', stop '$StopCron' ($TimeZone)" })
    apply = [bool]$Apply
} | ConvertTo-Json
if (-not $Apply) {
    Write-Host 'Dry run only. Re-run with -Apply to change the VM.'
    exit 0
}

if ($Remove) {
    if ($attached) {
        Invoke-Gcloud @('compute', 'instances', 'remove-resource-policies', $Instance, '--zone', $Zone,
            '--resource-policies', $PolicyName) | Out-Null
    }
    if ($hasShutdownScript) {
        Invoke-Gcloud @('compute', 'instances', 'remove-metadata', $Instance, '--zone', $Zone,
            '--keys', 'windows-shutdown-script-ps1') | Out-Null
    }
    Write-Host 'Schedule removed; the VM keeps its current power state.'
    exit 0
}

Invoke-Gcloud @('projects', 'add-iam-policy-binding', $Project, '--member', $agent,
    '--role', 'roles/compute.instanceAdmin.v1', '--condition', 'None', '--format', 'none') | Out-Null
if ($policy.ok) {
    $existing = ($policy.output | ConvertFrom-Json).instanceSchedulePolicy
    if ($existing.vmStartSchedule.schedule -ne $StartCron -or $existing.vmStopSchedule.schedule -ne $StopCron -or
        $existing.timeZone -ne $TimeZone) {
        throw "Policy $PolicyName exists with a different schedule; run with -Remove -Apply, delete it, then retry."
    }
} else {
    Invoke-Gcloud @('compute', 'resource-policies', 'create', 'instance-schedule', $PolicyName, '--region', $region,
        '--vm-start-schedule', $StartCron, '--vm-stop-schedule', $StopCron, '--timezone', $TimeZone,
        '--description', 'TTB competition: regular session only') | Out-Null
}
if (-not $attached) {
    Invoke-Gcloud @('compute', 'instances', 'add-resource-policies', $Instance, '--zone', $Zone,
        '--resource-policies', $PolicyName) | Out-Null
}
Invoke-Gcloud @('compute', 'instances', 'add-metadata', $Instance, '--zone', $Zone,
    '--metadata-from-file', "windows-shutdown-script-ps1=$shutdownScript") | Out-Null
Write-Host "Schedule active: start '$StartCron', stop '$StopCron' ($TimeZone)."
