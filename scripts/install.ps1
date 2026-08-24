<#
.SYNOPSIS
    One-shot installer for Sentinel NVR.

.DESCRIPTION
    Idempotent -- safe to re-run at any time.

    Steps performed:
      1. Verify this script is NOT running from C: (all data must go to F:).
      2. Create all required runtime directories on F:.
      3. Seed .env from .env.example if .env is missing, and generate a
         cryptographically strong SENTINEL_SECRET_KEY (base64 of 32 random bytes).
      4. Run fetch-binaries.ps1 (downloads go2rtc + FFmpeg, idempotent).
      5. Build the React frontend (npm run build inside frontend/).
      6. (Optional) Invoke install-service.ps1 if -InstallService is passed.
         That step REQUIRES elevation -- see note below.

    PARAMETERS
        -AppRoot        F:\sentinel\app     (default)
        -ToolsRoot      F:\sentinel\tools   (default; for node/npm)
        -DataRoot       F:\sentinel         (default; for runtime data dirs)
        -InstallService Switch -- if set, chain to install-service.ps1.
                        install-service.ps1 requires a separate elevated
                        PowerShell session -- this script will warn if not elevated.
        -SkipBuild      Switch -- skip npm build (useful in CI or dev).
        -SkipBinaries   Switch -- skip fetch-binaries.ps1 (already present).

.EXAMPLE
    # Normal first-time install (no service, no elevation needed):
    .\scripts\install.ps1

    # Install AND register Windows service (must run as Administrator):
    .\scripts\install.ps1 -InstallService

.NOTES
    - Node.js is expected at F:\sentinel\tools\node\npm.cmd (portable install).
    - Python venv is expected at F:\sentinel\app\.venv\Scripts\python.exe.
    - Nothing is written to C:. All state lives on F:.
#>

param(
    [string]$AppRoot       = "F:\sentinel\app",
    [string]$ToolsRoot     = "F:\sentinel\tools",
    [string]$DataRoot      = "F:\sentinel",
    [switch]$InstallService,
    [switch]$SkipBuild,
    [switch]$SkipBinaries
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
    Write-Host "   OK  $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "   WARN  $msg" -ForegroundColor Yellow
}

function Write-Info([string]$msg) {
    Write-Host "   $msg" -ForegroundColor Gray
}

function Require-OnFDrive([string]$path, [string]$label) {
    $resolved = [System.IO.Path]::GetFullPath($path)
    if ($resolved -like "C:\*") {
        Write-Error "FATAL: $label resolves to C: ($resolved). C: is nearly full. Aborting."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Step 0: Sanity -- ensure nothing would land on C:
# ---------------------------------------------------------------------------

Write-Step "Verifying paths are off C:"
Require-OnFDrive $AppRoot  "AppRoot"
Require-OnFDrive $ToolsRoot "ToolsRoot"
Require-OnFDrive $DataRoot  "DataRoot"
Write-Ok "All paths on non-C: drive."

# ---------------------------------------------------------------------------
# Step 1: Create runtime directories
# ---------------------------------------------------------------------------

Write-Step "Creating runtime directories under $DataRoot"

$dirs = @(
    "$DataRoot\recordings",
    "$DataRoot\events",
    "$DataRoot\data",
    "$DataRoot\logs"
)

foreach ($d in $dirs) {
    if (-not (Test-Path $d)) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        Write-Ok "Created: $d"
    } else {
        Write-Info "Exists:  $d"
    }
}

# ---------------------------------------------------------------------------
# Step 2: Seed .env if missing and inject strong secret key
# ---------------------------------------------------------------------------

Write-Step "Configuring .env"

$envFile     = Join-Path $AppRoot ".env"
$envExample  = Join-Path $AppRoot ".env.example"

if (-not (Test-Path $envFile)) {
    if (-not (Test-Path $envExample)) {
        Write-Error ".env.example not found at $envExample"
        exit 1
    }
    Copy-Item $envExample $envFile
    Write-Ok ".env seeded from .env.example"
} else {
    Write-Info ".env already exists -- not overwriting."
}

# Generate a strong secret key: base64 of 32 cryptographically random bytes.
$rng      = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$keyBytes = New-Object byte[] 32
$rng.GetBytes($keyBytes)
$secret   = [System.Convert]::ToBase64String($keyBytes)

# Replace SENTINEL_SECRET_KEY only if it still holds the placeholder value.
$envContent  = Get-Content $envFile -Raw -Encoding UTF8
$placeholder = "change-me-to-a-long-random-string"

if ($envContent -match [regex]::Escape($placeholder)) {
    $envContent = $envContent -replace [regex]::Escape($placeholder), $secret
    [System.IO.File]::WriteAllText($envFile, $envContent, [System.Text.Encoding]::UTF8)
    Write-Ok "SENTINEL_SECRET_KEY generated (base64 of 32 random bytes)."
} elseif ($envContent -match 'SENTINEL_SECRET_KEY=change-me') {
    $envContent = $envContent -replace 'SENTINEL_SECRET_KEY=change-me[^\r\n]*', "SENTINEL_SECRET_KEY=$secret"
    [System.IO.File]::WriteAllText($envFile, $envContent, [System.Text.Encoding]::UTF8)
    Write-Ok "SENTINEL_SECRET_KEY generated (base64 of 32 random bytes)."
} else {
    Write-Info "SENTINEL_SECRET_KEY already set (not regenerating)."
}

# ---------------------------------------------------------------------------
# Step 3: Fetch binaries (go2rtc + FFmpeg) -- idempotent
# ---------------------------------------------------------------------------

if (-not $SkipBinaries) {
    Write-Step "Fetching binaries (go2rtc + FFmpeg)"
    $fetchScript = Join-Path $AppRoot "scripts\fetch-binaries.ps1"
    if (-not (Test-Path $fetchScript)) {
        Write-Warn "fetch-binaries.ps1 not found at $fetchScript -- skipping."
    } else {
        & $fetchScript -ToolsRoot $ToolsRoot
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "fetch-binaries.ps1 exited with code $LASTEXITCODE"
        }
    }
} else {
    Write-Info "SkipBinaries flag set -- skipping binary fetch."
}

# ---------------------------------------------------------------------------
# Step 4: Build the frontend
# ---------------------------------------------------------------------------

if (-not $SkipBuild) {
    Write-Step "Building React frontend"

    $npmCmd      = Join-Path $ToolsRoot "node\npm.cmd"
    $nodeDir     = Join-Path $ToolsRoot "node"
    $frontendDir = Join-Path $AppRoot "frontend"

    if (-not (Test-Path $npmCmd)) {
        Write-Warn "npm not found at $npmCmd -- skipping frontend build."
        Write-Warn "Ensure Node.js (portable) is at $nodeDir"
    } elseif (-not (Test-Path $frontendDir)) {
        Write-Warn "Frontend directory not found: $frontendDir"
    } else {
        # Prepend node dir to PATH for this process.
        $env:PATH = "$nodeDir;" + $env:PATH

        Push-Location $frontendDir
        try {
            Write-Info "npm install..."
            & $npmCmd install --prefer-offline
            if ($LASTEXITCODE -ne 0) { Write-Error "npm install failed (exit $LASTEXITCODE)"; exit 1 }

            Write-Info "npm run build..."
            & $npmCmd run build
            if ($LASTEXITCODE -ne 0) { Write-Error "npm run build failed (exit $LASTEXITCODE)"; exit 1 }

            $distDir = Join-Path $frontendDir "dist"
            if (Test-Path $distDir) {
                Write-Ok "Frontend built: $distDir"
            } else {
                Write-Warn "dist/ not found after build -- check npm output above."
            }
        } finally {
            Pop-Location
        }
    }
} else {
    Write-Info "SkipBuild flag set -- skipping frontend build."
}

# ---------------------------------------------------------------------------
# Step 5: (Optional) Install Windows service
# ---------------------------------------------------------------------------

if ($InstallService) {
    Write-Step "Installing Windows service"

    # Check elevation
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    $elevated  = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $elevated) {
        Write-Warn "-InstallService was requested but this session is NOT elevated."
        Write-Warn "Re-run from an elevated PowerShell prompt (Run as administrator):"
        Write-Warn "  cd $AppRoot\scripts"
        Write-Warn "  .\install.ps1 -InstallService"
    } else {
        $svcScript = Join-Path $AppRoot "scripts\install-service.ps1"
        if (-not (Test-Path $svcScript)) {
            Write-Warn "install-service.ps1 not found at $svcScript"
        } else {
            & $svcScript
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "install-service.ps1 exited with code $LASTEXITCODE"
            }
        }
    }
} else {
    Write-Info "Service installation skipped. Use -InstallService (elevated) to install."
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host " Sentinel NVR installation complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host " To start the app (development / manual run):" -ForegroundColor Cyan
Write-Host "   cd $AppRoot"
Write-Host "   .\.venv\Scripts\python.exe -m uvicorn sentinel.main:app --host 127.0.0.1 --port 8080"
Write-Host ""
Write-Host " UI: http://127.0.0.1:8080" -ForegroundColor Cyan
Write-Host " Check the console for the first-run admin password." -ForegroundColor Yellow
Write-Host ""
Write-Host " For remote access, install Tailscale on this PC and your phone." -ForegroundColor Cyan
Write-Host " See README.md for full operator documentation." -ForegroundColor Cyan
Write-Host ""
