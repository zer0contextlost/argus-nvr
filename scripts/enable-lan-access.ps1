#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Create Windows Firewall inbound rules for Sentinel NVR LAN access.

.DESCRIPTION
    Adds two inbound firewall rules:
      1. "Sentinel NVR Web"     — TCP port 8080 (FastAPI / React UI / API)
      2. "Sentinel NVR WebRTC"  — TCP + UDP port 8555 (go2rtc WebRTC media)

    Run this script ONCE from an elevated (Run as Administrator) PowerShell
    session.  It is idempotent — existing rules with the same name are removed
    and re-added so re-running is safe.

    The go2rtc RTSP port (8554) is NOT opened by default because RTSP is only
    used locally by the FFmpeg recorder and the frame-grab detector.  Open it
    manually if you need to pull RTSP from another machine:
      New-NetFirewallRule -DisplayName "Sentinel NVR RTSP" -Direction Inbound `
        -Protocol TCP -LocalPort 8554 -Action Allow

.NOTES
    After running this script, access Sentinel from another LAN machine at:
      http://<this-PC-LAN-IP>:8080
    e.g. http://192.168.1.150:8080

    The app bind address must also be 0.0.0.0 (the default).  Check .env:
      SENTINEL_HOST=0.0.0.0

    For remote access over Tailscale you do NOT need this script — Tailscale
    creates its own virtual adapter and bypasses Windows Firewall for Tailscale
    traffic.  See README.md for Tailscale setup.
#>

[CmdletBinding(SupportsShouldProcess)]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Add-SentinelRule {
    param(
        [string]$DisplayName,
        [string]$Protocol,
        [int]$Port
    )

    # Remove existing rule with same name (idempotent).
    $existing = Get-NetFirewallRule -DisplayName $DisplayName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing rule: $DisplayName"
        Remove-NetFirewallRule -DisplayName $DisplayName
    }

    Write-Host "Creating rule: $DisplayName  Protocol=$Protocol  Port=$Port"
    New-NetFirewallRule `
        -DisplayName  $DisplayName `
        -Direction    Inbound `
        -Protocol     $Protocol `
        -LocalPort    $Port `
        -Action       Allow `
        -Profile      Any `
        -Enabled      True | Out-Null

    Write-Host "  OK"
}

Write-Host "=== Sentinel NVR — enabling LAN firewall rules ===" -ForegroundColor Cyan

# 1. FastAPI web UI / REST API — TCP 8080
Add-SentinelRule -DisplayName "Sentinel NVR Web"    -Protocol TCP -Port 8080

# 2. go2rtc WebRTC media — TCP 8555
Add-SentinelRule -DisplayName "Sentinel NVR WebRTC TCP" -Protocol TCP -Port 8555

# 3. go2rtc WebRTC media — UDP 8555
Add-SentinelRule -DisplayName "Sentinel NVR WebRTC UDP" -Protocol UDP -Port 8555

Write-Host ""
Write-Host "Done. Firewall rules created." -ForegroundColor Green
Write-Host ""
Write-Host "Access Sentinel from other LAN machines at:"
$lanIPs = (Get-NetIPAddress -AddressFamily IPv4 |
           Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.PrefixOrigin -ne 'WellKnown' } |
           Select-Object -ExpandProperty IPAddress)
foreach ($ip in $lanIPs) {
    Write-Host "  http://$ip`:8080" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Tip: ensure SENTINEL_HOST=0.0.0.0 is set in F:\sentinel\app\.env" -ForegroundColor Gray
