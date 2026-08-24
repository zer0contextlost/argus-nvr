# setup-build-env.ps1 -- one-time setup of the Windows build environment.
# Downloads and installs Inno Setup, bootstraps embedded Python, downloads binaries.
# Run ONCE before build-installer.ps1. Safe to re-run (idempotent).
#
# Usage (from installer\windows\):
#   .\setup-build-env.ps1
#
# After this completes, run:
#   .\build-installer.ps1 -SkipBinaries -SkipPython

param(
    [string]$NodePath  = (Join-Path $PSScriptRoot "..\..\..\tools\node"),
    [string]$IsccPath  = (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\iscc.exe")
)

$ErrorActionPreference = "Stop"
$ScriptDir  = $PSScriptRoot
$RepoRoot   = (Resolve-Path "$ScriptDir\..\.." ).Path
$CacheDir   = Join-Path $env:USERPROFILE ".cache\argus-build"
New-Item -ItemType Directory -Force $CacheDir | Out-Null

Write-Host "=== Argus NVR -- Build Environment Setup ==="
Write-Host

# ---- 1. Inno Setup ----------------------------------------------------------
Write-Host "[1/3] Inno Setup..."
if (Test-Path $IsccPath) {
    Write-Host "  already installed: $IsccPath"
} else {
    $isUrl   = "https://files.jrsoftware.org/is/6/innosetup-6.3.3.exe"
    $isCache = Join-Path $CacheDir "innosetup-6.3.3.exe"
    if (-not (Test-Path $isCache)) {
        Write-Host "  downloading Inno Setup 6.3.3..."
        Invoke-WebRequest $isUrl -OutFile $isCache -UseBasicParsing
    }
    Write-Host "  installing Inno Setup (silent)..."
    $proc = Start-Process $isCache -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-" -Wait -PassThru
    if ($proc.ExitCode -ne 0) { throw "Inno Setup install failed (exit $($proc.ExitCode))" }
    if (-not (Test-Path $IsccPath)) { throw "iscc.exe not found after install" }
    Write-Host "  installed: $IsccPath"
}

# ---- 2. Embedded Python + packages ------------------------------------------
Write-Host "[2/3] Embedded Python..."
$EmbedDir = Join-Path $ScriptDir "payload\python-embed"
if (Test-Path (Join-Path $EmbedDir "python.exe")) {
    $pkgCount = (Get-ChildItem (Join-Path $EmbedDir "Lib\site-packages") -Directory -ErrorAction SilentlyContinue | Measure-Object).Count
    Write-Host "  already bootstrapped - $pkgCount packages installed. Skipping."
} else {
    Write-Host "  bootstrapping embedded Python 3.11..."
    & (Join-Path $ScriptDir "bootstrap-embed-python.ps1")
}

# ---- 3. Tool binaries -------------------------------------------------------
Write-Host "[3/3] Tool binaries: go2rtc, FFmpeg, NSSM..."
$ToolsPayload = Join-Path $ScriptDir "payload\tools"
if ((Test-Path (Join-Path $ToolsPayload "go2rtc.exe")) -and
    (Test-Path (Join-Path $ToolsPayload "nssm.exe")) -and
    (Test-Path (Join-Path $ToolsPayload "ffmpeg\bin\ffmpeg.exe"))) {
    Write-Host "  already downloaded - skipping"
} else {
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
    & $pythonExe (Join-Path $RepoRoot "build\download-binaries.py") --os windows --dest $ToolsPayload
}

Write-Host
Write-Host "==================================================="
Write-Host " Build environment ready!"
Write-Host " Run: .\build-installer.ps1 -SkipBinaries -SkipPython"
Write-Host "==================================================="

