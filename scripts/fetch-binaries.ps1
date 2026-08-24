# fetch-binaries.ps1 — Download go2rtc.exe and ffmpeg.exe into F:\sentinel\tools
# (Already installed at tool paths; this script is for fresh installs on new machines.)

param(
    [string]$ToolsRoot = "F:\sentinel\tools",
    [string]$Go2rtcVersion = "v1.9.9",
    [string]$FfmpegBuild = "ffmpeg-release-essentials"
)

$ErrorActionPreference = "Stop"
Write-Host "Sentinel NVR — Binary Fetcher" -ForegroundColor Cyan

# --- go2rtc ---
$go2rtcDir  = "$ToolsRoot\go2rtc"
$go2rtcExe  = "$go2rtcDir\go2rtc.exe"
$go2rtcUrl  = "https://github.com/AlexxIT/go2rtc/releases/download/$Go2rtcVersion/go2rtc_win64.exe"

if (Test-Path $go2rtcExe) {
    Write-Host "go2rtc already present: $go2rtcExe" -ForegroundColor Green
} else {
    Write-Host "Downloading go2rtc $Go2rtcVersion..."
    New-Item -ItemType Directory -Force $go2rtcDir | Out-Null
    Invoke-WebRequest -Uri $go2rtcUrl -OutFile $go2rtcExe -UseBasicParsing
    Write-Host "go2rtc downloaded: $go2rtcExe" -ForegroundColor Green
}

# --- FFmpeg ---
$ffmpegDir = "$ToolsRoot\ffmpeg"
$ffmpegExe = "$ffmpegDir\bin\ffmpeg.exe"
$ffmpegUrl = "https://www.gyan.dev/ffmpeg/builds/$FfmpegBuild.zip"

if (Test-Path $ffmpegExe) {
    Write-Host "FFmpeg already present: $ffmpegExe" -ForegroundColor Green
} else {
    Write-Host "Downloading FFmpeg (this may take a while)..."
    $zipPath = "$ToolsRoot\ffmpeg.zip"
    Invoke-WebRequest -Uri $ffmpegUrl -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $ToolsRoot -Force
    # Rename the versioned directory.
    $extracted = Get-ChildItem $ToolsRoot -Directory | Where-Object { $_.Name -like "ffmpeg-*" } | Select-Object -First 1
    if ($extracted) {
        Rename-Item $extracted.FullName "ffmpeg"
    }
    Remove-Item $zipPath -Force
    Write-Host "FFmpeg downloaded: $ffmpegExe" -ForegroundColor Green
}

Write-Host ""
Write-Host "All binaries ready." -ForegroundColor Green
Write-Host "  go2rtc:  $go2rtcExe"
Write-Host "  FFmpeg:  $ffmpegExe"
