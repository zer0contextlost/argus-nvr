# service-uninstall.ps1 — stop and remove the Argus Windows Service.
# Called by Inno Setup [UninstallRun] section.

param([string]$InstallDir = "C:\Argus")

$nssm    = Join-Path $InstallDir "tools\nssm.exe"
$svcName = "Argus"

if (Test-Path $nssm) {
    Write-Host "Stopping and removing service '$svcName'..."
    & $nssm stop   $svcName confirm 2>$null
    & $nssm remove $svcName confirm 2>$null
} else {
    # NSSM gone already — try sc.exe fallback
    sc.exe stop   $svcName 2>$null
    sc.exe delete $svcName 2>$null
}

# Remove firewall rule
netsh advfirewall firewall delete rule name="Argus NVR" 2>$null | Out-Null

Write-Host "Service removed."
