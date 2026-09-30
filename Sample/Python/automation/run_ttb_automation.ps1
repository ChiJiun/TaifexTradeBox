param(
    [string]$WorkDir = 'C:\TTB\Sample\Python',
    [string]$ConfigPath = 'C:\TTB\Sample\Python\automation\config.json',
    [string]$TtbExe = 'C:\TTB\TTB.exe'
)

$ErrorActionPreference = 'Stop'
$statusDir = Join-Path $WorkDir 'output\supervisor'
$stopFile = Join-Path $statusDir 'stop.requested'
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

Set-Location -LiteralPath $WorkDir
Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
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

while (-not (Test-Path -LiteralPath $stopFile)) {
    if (-not (Get-Process -Name TTB -ErrorAction SilentlyContinue) -and (Test-Path -LiteralPath $TtbExe)) {
        Start-Process -FilePath $TtbExe
        Write-SupervisorLog 'TTB process started; interactive login may still be required'
    }
    if (-not (Test-LocalPort 8080) -or -not (Test-LocalPort 51141)) {
        Write-SupervisorLog 'waiting for TTB localhost ports 8080 and 51141'
        Start-Sleep -Seconds 15
        continue
    }

    $botArgs = @(
        'sim_hft_bot.py',
        '--symbol', [string]$config.symbol,
        '--mode', [string]$config.mode,
        '--strategy', [string]$config.strategy,
        '--health-file', (Join-Path $WorkDir 'output\health.json'),
        '--log-file', (Join-Path $statusDir 'bot.log')
    )
    if ($config.mode -eq 'ttb') {
        $botArgs += @('--confirm-virtual-orders', '--confirm-flat-account')
        if ($config.flattenOnShutdown) {
            $botArgs += '--flatten-on-shutdown'
        }
    }
    if ($config.extraArgs) {
        $botArgs += @($config.extraArgs | ForEach-Object { [string]$_ })
    }

    Write-SupervisorLog "launching bot mode=$($config.mode)"
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $pythonArgs = @('-3.13') + $botArgs
    $botStdout = Join-Path $statusDir 'bot.stdout.log'
    $botStderr = Join-Path $statusDir 'bot.stderr.log'
    $child = Start-Process `
        -FilePath 'py.exe' `
        -ArgumentList $pythonArgs `
        -WorkingDirectory $WorkDir `
        -RedirectStandardOutput $botStdout `
        -RedirectStandardError $botStderr `
        -NoNewWindow `
        -PassThru `
        -Wait
    $exitCode = $child.ExitCode
    Write-SupervisorLog "bot exited code=$exitCode"
    if ($config.mode -eq 'ttb' -and $exitCode -ne 0) {
        Set-Content -LiteralPath $reconcileFlag -Value (Get-Date -Format o) -Encoding ascii
        Write-SupervisorLog 'live virtual-order process will not auto-restart; manual order/position reconciliation required'
        break
    }
    Start-Sleep -Seconds 15
}

Write-SupervisorLog 'supervisor stopped'
