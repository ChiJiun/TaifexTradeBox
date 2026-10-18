<#
.SYNOPSIS
Leave the VM's RDP session without logging out or locking the TTB desktop.

.DESCRIPTION
Run inside the RDP session from an elevated PowerShell instead of closing the
RDP window. `tscon` moves the session to the VM console, so TTB keeps an
unlocked, rendered desktop and nothing waits on a disconnected session. Your
RDP window closes immediately; reconnect later with mstsc as usual.
#>
$ErrorActionPreference = 'Stop'
$sessionId = (Get-Process -Id $PID).SessionId
if ($sessionId -eq 0) { throw 'Run this inside the interactive RDP session, not as a service.' }
& tscon.exe $sessionId /dest:console
if ($LASTEXITCODE -ne 0) { throw "tscon failed with exit code $LASTEXITCODE (run PowerShell as Administrator)" }
