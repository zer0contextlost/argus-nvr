#!/usr/bin/env bash
# Argus NVR — uninstaller
# Usage: sudo ./uninstall.sh

set -euo pipefail

INSTALL_ROOT="/opt/argus"
SERVICE_NAME="argus"
SERVICE_FILE="/etc/systemd/system/argus.service"
UPDATE_SCRIPT="/usr/local/bin/argus-update"
ARGUS_USER="argus"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root (sudo ./uninstall.sh)" >&2
    exit 1
fi

echo "=== Argus NVR Uninstaller ==="

# Stop and disable service
echo "Stopping service..."
systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true

# Remove service file
rm -f "$SERVICE_FILE"
systemctl daemon-reload

# Remove update script and logrotate config
rm -f "$UPDATE_SCRIPT" /etc/logrotate.d/argus

# Remove app and tools
echo "Removing application files..."
rm -rf "$INSTALL_ROOT/app" "$INSTALL_ROOT/tools"

# Remove ufw rule if present
if command -v ufw &>/dev/null; then
    ufw delete allow 8080/tcp 2>/dev/null || true
fi

# Ask about data
echo
read -rp "Delete recordings, events, and database? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
    rm -rf \
        "$INSTALL_ROOT/data" \
        "$INSTALL_ROOT/recordings" \
        "$INSTALL_ROOT/events" \
        "$INSTALL_ROOT/logs" \
        /etc/argus
    echo "Data deleted."
else
    echo "Data preserved at $INSTALL_ROOT/{data,recordings,events} and /etc/argus/"
fi

# Remove install root if empty
rmdir "$INSTALL_ROOT" 2>/dev/null || true

echo "Argus NVR uninstalled."
