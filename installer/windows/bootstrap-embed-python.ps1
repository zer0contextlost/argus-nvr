# bootstrap-embed-python.ps1 -- download and prepare the Python 3.11 embeddable
# distribution for inclusion in the Windows installer payload.
#
# Run ONCE on the build machine before build-installer.ps1.
# Output: installer\windows\payload\python-embed\

param(
    [string]$PythonVersion = "3.11.9",
    [string]$ReqFile       = $null
)

$ErrorActionPreference = "Stop"

$ScriptDir   = $PSScriptRoot
$RepoRoot    = (Resolve-Path "$ScriptDir\..\.." ).Path
$PayloadDir  = Join-Path $ScriptDir "payload"
$EmbedDir    = Join-Path $PayloadDir "python-embed"
$CacheDir    = Join-Path $env:USERPROFILE ".cache\sentinel-build"

if (-not $ReqFile) { $ReqFile = Join-Path $RepoRoot "requirements.txt" }

$ZipUrl     = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl  = "https://bootstrap.pypa.io/get-pip.py"
$ZipCache   = Join-Path $CacheDir "python-$PythonVersion-embed-amd64.zip"
$GetPipPath = Join-Path $CacheDir "get-pip.py"

New-Item -ItemType Directory -Force $CacheDir  | Out-Null
New-Item -ItemType Directory -Force $PayloadDir | Out-Null

Write-Host "=== Python $PythonVersion Embeddable Bootstrap ==="

# 1. Download embeddable zip
if (Test-Path $ZipCache) {
    Write-Host "[1/5] Using cached: $ZipCache"
} else {
    Write-Host "[1/5] Downloading Python $PythonVersion embeddable..."
    Invoke-WebRequest $ZipUrl -OutFile $ZipCache -UseBasicParsing
    Write-Host "  saved: $ZipCache"
}

# 2. Extract
Write-Host "[2/5] Extracting to $EmbedDir..."
if (Test-Path $EmbedDir) { Remove-Item -Recurse -Force $EmbedDir }
New-Item -ItemType Directory -Force $EmbedDir | Out-Null
Expand-Archive -Path $ZipCache -DestinationPath $EmbedDir -Force

# 3. Patch python311._pth to enable site-packages
Write-Host "[3/5] Patching python311._pth..."
$pthFile = Join-Path $EmbedDir "python311._pth"
if (-not (Test-Path $pthFile)) {
    throw "python311._pth not found in $EmbedDir - check Python version"
}
$pth = Get-Content $pthFile -Raw
$pth = $pth -replace '#\s*import site', 'import site'
Set-Content $pthFile $pth -NoNewline

# 4. Bootstrap pip
Write-Host "[4/5] Bootstrapping pip..."
if (-not (Test-Path $GetPipPath)) {
    Invoke-WebRequest $GetPipUrl -OutFile $GetPipPath -UseBasicParsing
}
$py = Join-Path $EmbedDir "python.exe"
& $py $GetPipPath --quiet
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed" }
Write-Host "  pip installed"

# 5. Install requirements
Write-Host "[5/5] Installing requirements (this takes a few minutes)..."
& $py -m pip install --quiet --no-warn-script-location -r $ReqFile
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$sitePackages = Join-Path $EmbedDir "Lib\site-packages"
$pkgCount = (Get-ChildItem $sitePackages -Directory -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Host "  $pkgCount packages installed"

Write-Host ""
Write-Host "Done. Embedded Python ready at: $EmbedDir"
Write-Host "Next step: run build-installer.ps1"
