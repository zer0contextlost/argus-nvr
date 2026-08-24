# bootstrap.ps1 — post-install: create runtime dirs and generate .env.
# Python packages are pre-installed in the embedded Python at build time
# so no pip install is needed here.
# Called by Inno Setup [Run] section (or manually for testing).
# Args: $InstallDir

param([string]$InstallDir = "C:\Argus")

$ErrorActionPreference = "Stop"
$Python = Join-Path $InstallDir "app\python\python.exe"

Write-Host "Bootstrap: InstallDir=$InstallDir"

# 1. Create runtime directories
foreach ($d in @("data", "recordings", "events", "config", "logs")) {
    $path = Join-Path $InstallDir $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Force $path | Out-Null
        Write-Host "  created: $path"
    }
}

# 2. Generate .env if it doesn't exist (fresh install only; upgrade preserves it)
$envFile = Join-Path $InstallDir ".env"
if (-not (Test-Path $envFile)) {
    $genScript = Join-Path $InstallDir "app\installer\common\generate_env.py"
    & $Python $genScript $InstallDir $envFile
    Write-Host "  .env generated at $envFile"
} else {
    Write-Host "  .env exists — skipping (upgrade)"
}

Write-Host "Bootstrap complete."
