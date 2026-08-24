# service-install.ps1 — register the Argus Windows Service via NSSM.
# Called by Inno Setup [Run] section after bootstrap.ps1.
# Args: $InstallDir

param([string]$InstallDir = "C:\Argus")

$ErrorActionPreference = "Stop"
$nssm    = Join-Path $InstallDir "tools\nssm.exe"
$python  = Join-Path $InstallDir "app\python\python.exe"
$appDir  = Join-Path $InstallDir "app"
$envFile = Join-Path $InstallDir ".env"
$logDir  = Join-Path $InstallDir "logs"
$svcName = "Argus"

Write-Host "Registering Windows Service '$svcName'..."

# Stop + remove if it already exists (upgrade)
$existing = Get-Service -Name $svcName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  existing service found — removing"
    & $nssm stop  $svcName confirm 2>$null
    & $nssm remove $svcName confirm
}

# Install
& $nssm install $svcName $python
& $nssm set $svcName AppParameters "-m uvicorn sentinel.main:app --host 0.0.0.0 --port 8080"
& $nssm set $svcName AppDirectory   $appDir
& $nssm set $svcName AppEnvironmentExtra "ARGUS_ENV_FILE=$envFile"
& $nssm set $svcName Start          SERVICE_AUTO_START
& $nssm set $svcName AppStdout      "$logDir\stdout.log"
& $nssm set $svcName AppStderr      "$logDir\stderr.log"
& $nssm set $svcName AppRotateFiles 1
& $nssm set $svcName AppRotateBytes 10485760
& $nssm set $svcName AppExit        Default Restart
& $nssm set $svcName AppRestartDelay 5000
& $nssm set $svcName Description    "Argus NVR — self-hosted IP camera recorder"

# Firewall rule (TCP 8080 for the web UI)
$ruleName = "Argus NVR"
$existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existingRule) {
    netsh advfirewall firewall add rule `
        name=$ruleName dir=in action=allow protocol=TCP localport=8080 `
        description="Argus NVR web interface" | Out-Null
    Write-Host "  firewall rule added (TCP 8080)"
}

# Start service
& $nssm start $svcName
Write-Host "Service '$svcName' started."
