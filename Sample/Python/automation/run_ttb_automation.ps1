param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$ConfigPath = 'C:\TTB\Sample\Python\automation\config.json',
    [string]$TtbExe = 'C:\TTB\TTB.exe'
)

$ErrorActionPreference = 'Stop'
$statusDir = Join-Path $WorkDir 'output\supervisor'
$stopFile = Join-Path $statusDir 'stop.requested'
$botStopFile = Join-Path $statusDir 'bot-stop.requested'
$reconcileFlag = Join-Path $statusDir 'manual-reconciliation-required.flag'
$supervisorLog = Join-Path $statusDir 'supervisor.log'
New-Item -ItemType Directory -Path $statusDir -Force | Out-Null

function Write-SupervisorLog([string]$Message) {
    if ((Test-Path -LiteralPath $supervisorLog) -and (Get-Item -LiteralPath $supervisorLog).Length -gt 10MB) {
        $archive = Join-Path $statusDir ('supervisor-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Move-Item -LiteralPath $supervisorLog -Destination $archive -Force
    }
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $supervisorLog -Value $line -Encoding utf8
}

function Test-LocalPort([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Invoke-PythonTool([string]$Name, [string[]]$Arguments) {
    # Helper output is JSON without secrets; keep it in the supervisor log.
    # Windows PowerShell 5.1 turns native stderr into a terminating error
    # under 'Stop', which would kill the supervisor; exit codes decide here.
    $ErrorActionPreference = 'Continue'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $output = & py.exe -3.13 @Arguments 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-SupervisorLog ("{0} exit={1} {2}" -f $Name, $code, ($output.Trim() -replace '\s+', ' '))
    return $code
}

function Invoke-TtbLogin {
    if (-not $config.autoLogin) { return 0 }
    $ErrorActionPreference = 'Continue'
    $loginArgs = @('-m', 'automation.ttb_login', '--config', $ConfigPath, '--ttb-exe', $TtbExe)
    # TTB only publishes ticks for symbols in its quote-settings tab; report
    # (read-only) whether the resolved contract is subscribed.
    $resolved = (& py.exe -3.13 -m automation.resolve_symbol ([string]$config.symbol) 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $resolved) { $loginArgs += @('--subscribe', $resolved) }
    return Invoke-PythonTool 'ttb_login' $loginArgs
}

function Test-AutoRecoveryAllowed {
    return ($config.mode -eq 'ttb' -and $config.autoRecovery -and $config.autoRecovery.enabled -and
        $config.autoRecovery.exclusiveBotAccount)
}

function Invoke-AutoRecovery {
    $toolArgs = @('auto_recover_ttb.py', '--confirm-exclusive-bot-account')
    if ($config.autoRecovery.maxAttemptsPerDay) {
        $toolArgs += @('--max-attempts-per-day', [string]$config.autoRecovery.maxAttemptsPerDay)
    }
    if ($config.autoRecovery.maxFlattenQty) {
        $toolArgs += @('--max-flatten-qty', [string]$config.autoRecovery.maxFlattenQty)
    }
    return Invoke-PythonTool 'auto_recover_ttb' $toolArgs
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Missing automation config: $ConfigPath. Copy config.example.json and review it first."
}
$config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
if ($config.mode -notin @('shadow', 'ttb')) {
    throw 'mode must be shadow or ttb'
}
if ($config.mode -eq 'ttb' -and (-not $config.confirmVirtualOrders -or -not $config.confirmFlatAccount)) {
    throw 'ttb mode requires confirmVirtualOrders=true and confirmFlatAccount=true'
}
# A scheduled/logon restart must never override an unresolved live failure.
# The existing preflight check is not necessarily run by Task Scheduler.
# Only the audited, bounded auto-recovery (exclusive bot account) may clear it.
if ($config.mode -eq 'ttb' -and (Test-Path -LiteralPath $reconcileFlag) -and -not (Test-AutoRecoveryAllowed)) {
    Write-SupervisorLog 'refusing live startup: manual reconciliation flag exists'
    throw "Refusing live startup until manual reconciliation is completed: $reconcileFlag"
}
if ($config.mode -eq 'ttb' -and (Test-Path -LiteralPath $stopFile)) {
    Write-SupervisorLog 'refusing live startup: stop request still exists'
    throw "Refusing live startup while a stop request is present: $stopFile"
}

Set-Location -LiteralPath $WorkDir
if ($config.mode -ne 'ttb') {
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
}
Write-SupervisorLog "supervisor starting symbol=$($config.symbol) mode=$($config.mode)"
$cleanupScript = Join-Path $WorkDir 'automation\cleanup_runtime.ps1'
if (Test-Path -LiteralPath $cleanupScript) {
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $cleanupScript -WorkDir $WorkDir | Out-Null
        Write-SupervisorLog 'runtime retention cleanup completed'
    } catch {
        Write-SupervisorLog "runtime retention cleanup failed: $($_.Exception.Message)"
    }
}

# After TTB's server drops the competition selection, the GUI still shows it,
# so login never reselects and the API answers 500 forever (10/19, 64 hours).
$maxLoginFailuresBeforeRestart = 3
$loginFailures = 0
while (-not (Test-Path -LiteralPath $stopFile)) {
    if (-not (Get-Process -Name TTB -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $TtbExe)) {
        Start-Process -FilePath $TtbExe -WorkingDirectory (Split-Path -Parent $TtbExe)
        Write-SupervisorLog 'TTB process started; login required'
        Start-Sleep -Seconds 10
    }
    # TTB opens its local API ports only after login, so log in first.
    if ((Invoke-TtbLogin) -ne 0) {
        $loginFailures += 1
        if ($loginFailures -ge $maxLoginFailuresBeforeRestart) {
            Write-SupervisorLog "TTB login failed $loginFailures times in a row; restarting TTB"
            Stop-Process -Name TTB -Force -ErrorAction SilentlyContinue
            $loginFailures = 0
            Start-Sleep -Seconds 5
            continue
        }
        Write-SupervisorLog 'TTB is not logged in to the competition; retrying in 60s'
        Start-Sleep -Seconds 60
        continue
    }
    $loginFailures = 0
    if (-not (Test-LocalPort 8080) -or -not (Test-LocalPort 51141)) {
        Write-SupervisorLog 'waiting for TTB localhost ports 8080 and 51141'
        Start-Sleep -Seconds 15
        continue
    }
    if ($config.mode -eq 'ttb' -and (Test-Path -LiteralPath $reconcileFlag)) {
        if (-not (Test-AutoRecoveryAllowed)) {
            Write-SupervisorLog 'refusing further live launches: manual reconciliation flag exists'
            break
        }
        $recovery = Invoke-AutoRecovery
        if ($recovery -eq 3) {
            Write-SupervisorLog 'auto-recovery deferred (market closed or reports unavailable); retrying in 60s'
            Start-Sleep -Seconds 60
            continue
        }
        if ($recovery -ne 0) {
            Write-SupervisorLog 'auto-recovery refused; manual reconciliation required (see OPERATOR_RUNBOOK.md)'
            break
        }
        Write-SupervisorLog 'auto-recovery verified a clean account and cleared the flag'
    }

    if ($config.mode -eq 'ttb' -and ($null -eq $config.liveGate -or $config.liveGate.probeBeforeFirstLive -ne $false)) {
        # One far-limit order/cancel probe per contract before strategy orders.
        $gate = Invoke-PythonTool 'live_gate' @('-m', 'automation.live_gate', '--symbol', [string]$config.symbol)
        if ($gate -eq 3) {
            Write-SupervisorLog 'live gate deferred (market closed or no quote yet); retrying in 60s'
            Start-Sleep -Seconds 60
            continue
        }
        if ($gate -ne 0) {
            Set-Content -LiteralPath $reconcileFlag -Value (Get-Date -Format o) -Encoding ascii
            Write-SupervisorLog 'live gate failed: one-lot probe did not verify cleanly; manual check required'
            break
        }
    }

    $botArgs = @(
        'sim_hft_bot.py',
        '--symbol', [string]$config.symbol,
        '--mode', [string]$config.mode,
        '--strategy', [string]$config.strategy,
        '--stop-file', $botStopFile,
        '--health-file', (Join-Path $WorkDir 'output\health.json'),
        '--log-file', (Join-Path $statusDir 'bot.log')
    )
    if ($config.strategyModule) {
        $botArgs += @('--strategy-module', [string]$config.strategyModule)
    }
    if ($config.mode -eq 'ttb') {
        $botArgs += @('--confirm-virtual-orders', '--confirm-flat-account')
        if ($config.flattenOnShutdown) {
            $botArgs += '--flatten-on-shutdown'
        }
    }
    if ($config.extraArgs) {
        $botArgs += @($config.extraArgs | ForEach-Object { [string]$_ })
    }

    $strategyName = if ($config.strategyModule) { $config.strategyModule } else { $config.strategy }
    Write-SupervisorLog "launching bot mode=$($config.mode) strategy=$strategyName"
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $pythonArgs = @('-3.13') + $botArgs
    $botStdout = Join-Path $statusDir 'bot.stdout.log'
    $botStderr = Join-Path $statusDir 'bot.stderr.log'
    Remove-Item -LiteralPath $botStopFile -Force -ErrorAction SilentlyContinue
    $child = Start-Process `
        -FilePath 'py.exe' `
        -ArgumentList $pythonArgs `
        -WorkingDirectory $WorkDir `
        -RedirectStandardOutput $botStdout `
        -RedirectStandardError $botStderr `
        -NoNewWindow `
        -PassThru
    $null = $child.Handle  # keeps ExitCode available after the process ends
    # The bot cannot notice TTB disappearing while the market is closed, so
    # ask it to stop gracefully whenever TTB exits or a stop is requested.
    while (-not $child.WaitForExit(5000)) {
        $ttbGone = -not (Get-Process -Name TTB -ErrorAction SilentlyContinue)
        if (($ttbGone -or (Test-Path -LiteralPath $stopFile)) -and -not (Test-Path -LiteralPath $botStopFile)) {
            Set-Content -LiteralPath $botStopFile -Value (Get-Date -Format o) -Encoding ascii
            Write-SupervisorLog ("asking bot to stop: {0}" -f $(if ($ttbGone) { 'TTB process exited' } else { 'stop requested' }))
        }
    }
    $exitCode = $child.ExitCode
    Write-SupervisorLog "bot exited code=$exitCode"
    if ($config.mode -eq 'ttb' -and $exitCode -ne 0) {
        Set-Content -LiteralPath $reconcileFlag -Value (Get-Date -Format o) -Encoding ascii
        if (-not (Test-AutoRecoveryAllowed)) {
            Write-SupervisorLog 'live virtual-order process will not auto-restart; manual order/position reconciliation required'
            break
        }
        Write-SupervisorLog 'live bot failed; bounded auto-recovery will reconcile before any relaunch'
    }
    Start-Sleep -Seconds 15
}

Write-SupervisorLog 'supervisor stopped'
