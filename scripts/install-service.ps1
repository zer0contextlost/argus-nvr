#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install Sentinel NVR as an auto-start Windows service using NSSM.

.DESCRIPTION
    This script installs Sentinel NVR (uvicorn via the venv Python) as a
    Windows service that starts automatically with Windows and auto-restarts
    on failure.

    NSSM (Non-Sucking Service Manager) is downloaded automatically into
    F:\sentinel\tools\nssm\ if it is not already present.

    Fallback: if NSSM cannot be downloaded, a Task Scheduler at-startup task
    is registered instead.

    HOW TO RUN (must be elevated):
        Right-click PowerShell → "Run as administrator"
        cd F:\sentinel\app\scripts
        .\install-service.ps1

    OPTIONAL PARAMETERS:
        -ServiceName   "SentinelNVR"         (default)
        -AppRoot       "F:\sentinel\app"      (default)
        -Python        "F:\sentinel\app\.venv\Scripts\python.exe" (default)
        -LogDir        "F:\sentinel\logs"     (default)
        -NssmDir       "F:\sentinel\tools\nssm" (default)
        -Host          "127.0.0.1"            (default — Tailscale IP or 0.0.0.0 for LAN)
        -Port          8080                   (default)

.NOTES
    After running this script the service starts automatically.
    Manage it with:
        Get-Service SentinelNVR
        Start-Service SentinelNVR
        Stop-Service SentinelNVR
        & "F:\sentinel\tools\nssm\nssm.exe" edit SentinelNVR   # GUI editor

    To uninstall run:
        .\uninstall-service.ps1
#>

param(
    [string]$ServiceName = "SentinelNVR",
    [string]$AppRoot     = "F:\sentinel\app",
    [string]$Python      = "F:\sentinel\app\.venv\Scripts\python.exe",
    [string]$LogDir      = "F:\sentinel\logs",
    [string]$NssmDir     = "F:\sentinel\tools\nssm",
    [string]$Host        = "127.0.0.1",
    [int]   $Port        = 8080
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Step([string]$msg) {
    Write-Host "`n>> $msg" -ForegroundColor Cyan
}

function Write-Ok([string]$msg) {
    Write-Host "   OK: $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "   WARN: $msg" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# Validate prerequisites
# ---------------------------------------------------------------------------

Write-Step "Checking prerequisites"

if (-not (Test-Path $Python)) {
    Write-Error "Python venv not found at: $Python"
    exit 1
}
Write-Ok "Python: $Python"

if (-not (Test-Path $AppRoot)) {
    Write-Error "App root not found: $AppRoot"
    exit 1
}
Write-Ok "App root: $AppRoot"

# Ensure log directory exists
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}
Write-Ok "Log dir: $LogDir"

# ---------------------------------------------------------------------------
# Download NSSM if needed
# ---------------------------------------------------------------------------

Write-Step "Locating NSSM"

$NssmExe = Join-Path $NssmDir "nssm.exe"
$useNssm = $false

if (Test-Path $NssmExe) {
    Write-Ok "NSSM found: $NssmExe"
    $useNssm = $true
} else {
    Write-Host "   NSSM not found — attempting download..." -ForegroundColor Yellow
    try {
        if (-not (Test-Path $NssmDir)) {
            New-Item -ItemType Directory -Force -Path $NssmDir | Out-Null
        }
        # NSSM 2.24 stable release
        $nssmZipUrl = "https://nssm.cc/release/nssm-2.24.zip"
        $nssmZip    = Join-Path $NssmDir "nssm.zip"

        $wc = New-Object System.Net.WebClient
        $wc.DownloadFile($nssmZipUrl, $nssmZip)

        Expand-Archive -Path $nssmZip -DestinationPath $NssmDir -Force

        # The zip contains nssm-2.24\win64\nssm.exe
        $extracted = Get-ChildItem -Path $NssmDir -Recurse -Filter "nssm.exe" |
                     Where-Object { $_.FullName -like "*win64*" } |
                     Select-Object -First 1

        if ($extracted) {
            Copy-Item $extracted.FullName -Destination $NssmExe
            Remove-Item $nssmZip -Force
            Write-Ok "NSSM downloaded to $NssmExe"
            $useNssm = $true
        } else {
            Write-Warn "Could not find nssm.exe in the downloaded archive."
        }
    } catch {
        Write-Warn "NSSM download failed: $_"
    }
}

# ---------------------------------------------------------------------------
# Build uvicorn command
# ---------------------------------------------------------------------------

$uvicornArgs = "-m uvicorn sentinel.main:app --host $Host --port $Port"
$stdoutLog   = Join-Path $LogDir "sentinel-stdout.log"
$stderrLog   = Join-Path $LogDir "sentinel-stderr.log"

# ---------------------------------------------------------------------------
# Install via NSSM (preferred)
# ---------------------------------------------------------------------------

if ($useNssm) {
    Write-Step "Installing Windows service '$ServiceName' via NSSM"

    # Remove existing service if present (idempotent).
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Warn "Service '$ServiceName' already exists — removing first."
        & $NssmExe stop $ServiceName 2>$null
        & $NssmExe remove $ServiceName confirm
    }

    # Install
    & $NssmExe install $ServiceName $Python
    if ($LASTEXITCODE -ne 0) { Write-Error "NSSM install failed"; exit 1 }

    # Configure
    & $NssmExe set $ServiceName AppParameters  $uvicornArgs
    & $NssmExe set $ServiceName AppDirectory   $AppRoot
    & $NssmExe set $ServiceName Start          SERVICE_AUTO_START
    & $NssmExe set $ServiceName AppRestartDelay 5000
    & $NssmExe set $ServiceName AppStdout      $stdoutLog
    & $NssmExe set $ServiceName AppStderr      $stderrLog
    & $NssmExe set $ServiceName AppRotateFiles 1
    & $NssmExe set $ServiceName AppRotateBytes 10485760   # 10 MB
    & $NssmExe set $ServiceName DisplayName    "Sentinel NVR"
    & $NssmExe set $ServiceName Description    "Sentinel NVR — self-hosted camera surveillance"

    # Set recovery actions: restart on failure after 5 s, 3 times.
    & $NssmExe set $ServiceName AppExit         Default Restart
    & $NssmExe set $ServiceName AppThrottle     5000

    Write-Ok "Service configured"

    Write-Step "Starting service '$ServiceName'"
    Start-Service -Name $ServiceName
    Write-Ok "Service started. Check status with: Get-Service $ServiceName"

} else {
    # -----------------------------------------------------------------------
    # Fallback: Task Scheduler at-startup task
    # -----------------------------------------------------------------------
    Write-Step "NSSM unavailable — registering Task Scheduler startup task (fallback)"

    $taskName    = $ServiceName
    $taskScript  = "$Python $uvicornArgs"
    $action      = New-ScheduledTaskAction `
                        -Execute $Python `
                        -Argument $uvicornArgs `
                        -WorkingDirectory $AppRoot

    $trigger     = New-ScheduledTaskTrigger -AtStartup
    $principal   = New-ScheduledTaskPrincipal `
                        -UserId "SYSTEM" `
                        -LogonType ServiceAccount `
                        -RunLevel Highest
    $settings    = New-ScheduledTaskSettingsSet `
                        -MultipleInstances IgnoreNew `
                        -RestartCount 5 `
                        -RestartInterval (New-TimeSpan -Minutes 1) `
                        -ExecutionTimeLimit ([TimeSpan]::Zero)

    # Remove existing task if present.
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask `
        -TaskName  $taskName `
        -Action    $action `
        -Trigger   $trigger `
        -Principal $principal `
        -Settings  $settings `
        -Description "Sentinel NVR auto-start (Task Scheduler fallback)" | Out-Null

    Write-Ok "Task '$taskName' registered — will run as SYSTEM at startup."
    Write-Warn "Note: Task Scheduler does not auto-restart as reliably as NSSM."
    Write-Warn "Install NSSM manually from https://nssm.cc/ for better reliability."

    # Start it now.
    Start-ScheduledTask -TaskName $taskName
    Write-Ok "Task started. Check with: Get-ScheduledTask -TaskName $taskName"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host " Sentinel NVR service installation complete." -ForegroundColor Green
Write-Host " UI will be available at: http://${Host}:${Port}" -ForegroundColor Green
Write-Host " Logs: $LogDir" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green

