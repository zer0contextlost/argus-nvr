# build-installer.ps1 -- assemble the Windows installer and run Inno Setup.
#
# Prerequisites (run setup-build-env.ps1 to handle all of these automatically):
#   1. Inno Setup 6 installed
#   2. bootstrap-embed-python.ps1 already run (payload\python-embed\ exists)
#   3. go2rtc + ffmpeg + nssm downloaded (payload\tools\ exists)
#   4. Frontend built (frontend\dist\ exists)
#
# Quick start (from repo root):
#   .\installer\windows\setup-build-env.ps1
#   .\installer\windows\build-installer.ps1
#
# Incremental rebuild (skip slow steps):
#   .\installer\windows\build-installer.ps1 -SkipFrontend -SkipBinaries -SkipPython

param(
    [string]$Version       = "1.0.0",
    [string]$NodePath      = "F:\sentinel\tools\node",
    [string]$IsccPath      = "$env:LOCALAPPDATA\Programs\Inno Setup 6\iscc.exe",
    [switch]$SkipFrontend  = $false,
    [switch]$SkipBinaries  = $false,
    [switch]$SkipPython    = $false
)

$ErrorActionPreference = "Stop"

$ScriptDir    = $PSScriptRoot
$RepoRoot     = (Resolve-Path "$ScriptDir\..\.." ).Path
$PayloadDir   = Join-Path $ScriptDir "payload"
$PayloadApp   = Join-Path $PayloadDir "app"
$PayloadTools = Join-Path $PayloadDir "tools"
$EmbedDir     = Join-Path $PayloadDir "python-embed"
$DistDir      = Join-Path $RepoRoot "dist"

Write-Host "=== Argus NVR Windows Installer Build v$Version ==="
Write-Host "Repo root:  $RepoRoot"
Write-Host "Output dir: $DistDir"
Write-Host ""

# ---- Step 1: Frontend -------------------------------------------------------
if ($SkipFrontend) {
    Write-Host "[1/5] Skipping frontend build"
} else {
    Write-Host "[1/5] Building frontend..."
    & (Join-Path $RepoRoot "build\build-frontend.ps1") -NodePath $NodePath
}

# ---- Step 2: Binaries -------------------------------------------------------
if ($SkipBinaries) {
    Write-Host "[2/5] Skipping binary download"
} else {
    Write-Host "[2/5] Downloading tool binaries..."
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
    & $pythonExe (Join-Path $RepoRoot "build\download-binaries.py") --os windows --dest $PayloadTools
}

# ---- Step 3: Embedded Python ------------------------------------------------
if ($SkipPython) {
    Write-Host "[3/5] Skipping embedded Python"
} else {
    $embedPy = Join-Path $EmbedDir "python.exe"
    if (Test-Path $embedPy) {
        Write-Host "[3/5] Embedded Python already bootstrapped - skipping"
    } else {
        Write-Host "[3/5] Bootstrapping embedded Python..."
        & (Join-Path $ScriptDir "bootstrap-embed-python.ps1")
    }
}

$embedPy = Join-Path $EmbedDir "python.exe"
if (-not (Test-Path $embedPy)) {
    Write-Host "ERROR: payload\python-embed\python.exe not found."
    Write-Host "  Run: .\installer\windows\bootstrap-embed-python.ps1"
    exit 1
}

# ---- Step 4: Stage payload --------------------------------------------------
Write-Host "[4/5] Staging app payload..."
if (Test-Path $PayloadApp) { Remove-Item -Recurse -Force $PayloadApp }
New-Item -ItemType Directory -Force $PayloadApp | Out-Null

Copy-Item -Recurse -Force (Join-Path $RepoRoot "sentinel")                      "$PayloadApp\sentinel"
# Only copy installer\common (env template + generate_env.py) -- not the full
# installer tree which contains the payload dir itself and would recurse.
New-Item -ItemType Directory -Force "$PayloadApp\installer\common" | Out-Null
Copy-Item -Force (Join-Path $RepoRoot "installer\common\env.template")   "$PayloadApp\installer\common\"
Copy-Item -Force (Join-Path $RepoRoot "installer\common\generate_env.py") "$PayloadApp\installer\common\"
# Windows service scripts needed at install time
New-Item -ItemType Directory -Force "$PayloadApp\installer\windows" | Out-Null
Copy-Item -Force (Join-Path $ScriptDir "service-install.ps1")   "$PayloadApp\installer\windows\"
Copy-Item -Force (Join-Path $ScriptDir "service-uninstall.ps1") "$PayloadApp\installer\windows\"
Copy-Item               (Join-Path $RepoRoot "requirements.txt")          $PayloadApp
Copy-Item               (Join-Path $RepoRoot "requirements-face.txt")     $PayloadApp

New-Item -ItemType Directory -Force "$PayloadApp\frontend" | Out-Null
Copy-Item -Recurse -Force (Join-Path $RepoRoot "frontend\dist")       "$PayloadApp\frontend\dist"

Copy-Item -Recurse -Force $EmbedDir "$PayloadApp\python"

Set-Content -Path "$PayloadApp\VERSION" -Value $Version -Encoding UTF8

Write-Host "  payload staged at $PayloadApp"

# ---- Step 5: Run Inno Setup -------------------------------------------------
Write-Host "[5/5] Building installer..."
if (-not (Test-Path $IsccPath)) {
    Write-Host "ERROR: Inno Setup not found at: $IsccPath"
    Write-Host "Install via: winget install JRSoftware.InnoSetup"
    exit 1
}

New-Item -ItemType Directory -Force $DistDir | Out-Null
Push-Location $ScriptDir
try {
    & $IsccPath "/DAppVersion=$Version" "/O$DistDir" "sentinel.iss"
    if ($LASTEXITCODE -ne 0) { throw "iscc failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

$exePath = Join-Path $DistDir "Argus-Setup-$Version.exe"
Write-Host ""
Write-Host "==================================================="
Write-Host " Installer ready:"
Write-Host "  $exePath"
Write-Host "==================================================="
