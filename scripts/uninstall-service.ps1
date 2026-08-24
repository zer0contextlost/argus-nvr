#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Uninstall the Sentinel NVR Windows service (NSSM) or Task Scheduler task.

.DESCRIPTION
    Stops and removes the SentinelNVR service installed by install-service.ps1.
    Works whether the service was installed via NSSM or Task Scheduler fallback.

.NOTES
    HOW TO RUN (must be elevated):
        Right-click PowerShell -> "Run as administrator"
        cd F:\sentinel\app\scripts
        .\uninstall-service.ps1
#>

param(
    [string]$ServiceName = "SentinelNVR",
    [string]$NssmDir     = "F:\sentinel\tools\nssm"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

function Write-Step([string]$msg) {
    Write-Host "`n>> $msg" -ForegroundColor Cyan
}
function Write-Ok([string]$msg) {
    Write-Host "   OK: $msg" -ForegroundColor Green
}
function Write-Warn([string]$msg) {
    Write-Host "   WARN: $msg" -ForegroundColor Yellow
}

$NssmExe = Join-Path $NssmDir "nssm.exe"

# ---------------------------------------------------------------------------
# Try NSSM uninstall first
# ---------------------------------------------------------------------------

Write-Step "Checking for NSSM-managed service '$ServiceName'"

if (Test-Path $NssmExe) {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "   Stopping service..." -ForegroundColor Yellow
        & $NssmExe stop $ServiceName
        Write-Host "   Removing service..." -ForegroundColor Yellow
        & $NssmExe remove $ServiceName confirm
        Write-Ok "NSSM service '$ServiceName' removed."
    } else {
        Write-Warn "No Windows service named '$ServiceName' found."
    }
} else {
    # sc.exe fallback (no NSSM)
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        & sc.exe delete $ServiceName
        Write-Ok "Service '$ServiceName' removed via sc.exe."
    } else {
        Write-Warn "No Windows service named '$ServiceName' found."
    }
}

# ---------------------------------------------------------------------------
# Remove Task Scheduler fallback if present
# ---------------------------------------------------------------------------

Write-Step "Checking for Task Scheduler task '$ServiceName'"

$task = Get-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $ServiceName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $ServiceName -Confirm:$false
    Write-Ok "Scheduled task '$ServiceName' removed."
} else {
    Write-Warn "No scheduled task named '$ServiceName' found."
}

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host " Sentinel NVR service uninstallation complete." -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
