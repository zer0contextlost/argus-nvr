#!/usr/bin/env bash
# argus-update — upgrade Argus NVR to a new version.
# Installed at /usr/local/bin/argus-update by install.sh.
#
# Usage:
#   sudo argus-update                          # download latest from release URL
#   sudo argus-update --from /path/to.tar.gz  # install from a local tarball

set -euo pipefail

INSTALL_ROOT="/opt/argus"
APP_DIR="$INSTALL_ROOT/app"
SERVICE_NAME="argus"
ARGUS_USER="argus"

# Update this URL when a new release is published.
RELEASE_URL="https://github.com/zer0contextlost/argus-nvr/releases/latest/download/argus-linux-x86_64.tar.gz"

TARBALL=""
if [[ "${1:-}" == "--from" && -n "${2:-}" ]]; then
    TARBALL="$2"
fi

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root (sudo argus-update)" >&2
    exit 1
fi

# Download if no local tarball was provided
if [[ -z "$TARBALL" ]]; then
    TARBALL="/tmp/argus-update-$$.tar.gz"
    echo "Downloading from $RELEASE_URL..."
    curl -fsSL -o "$TARBALL" "$RELEASE_URL"
fi

[[ -f "$TARBALL" ]] || { echo "ERROR: tarball not found: $TARBALL" >&2; exit 1; }

echo "Stopping service..."
systemctl stop "$SERVICE_NAME"

# Backup current app dir
if [[ -d "$APP_DIR" ]]; then
    mv "$APP_DIR" "${APP_DIR}.bak"
    echo "Backup: ${APP_DIR}.bak"
fi

# Extract new version
echo "Extracting..."
mkdir -p "$APP_DIR"
tar xzf "$TARBALL" -C "$APP_DIR" --strip-components=1 2>/dev/null || \
tar xzf "$TARBALL" -C "$APP_DIR"

# Update tools if present in tarball
TOOLS_DIR="$INSTALL_ROOT/tools"
if [[ -d "$APP_DIR/tools" ]]; then
    rsync -a --delete "$APP_DIR/tools/" "$TOOLS_DIR/"
    chmod +x "$TOOLS_DIR/go2rtc" "$TOOLS_DIR/ffmpeg" "$TOOLS_DIR/ffprobe" 2>/dev/null || true
    rm -rf "$APP_DIR/tools"
fi

# Reinstall Python dependencies
echo "Updating dependencies..."
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# Re-set ownership
chown -R "$ARGUS_USER:$ARGUS_USER" "$INSTALL_ROOT"

echo "Starting service..."
systemctl start "$SERVICE_NAME"

# Verify it came back up
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    rm -rf "${APP_DIR}.bak" 2>/dev/null || true
    echo "Upgrade complete. Service is running."
else
    echo "ERROR: Service failed to start after upgrade. Rolling back..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    rm -rf "$APP_DIR"
    mv "${APP_DIR}.bak" "$APP_DIR"
    chown -R "$ARGUS_USER:$ARGUS_USER" "$APP_DIR"
    systemctl start "$SERVICE_NAME"
    echo "Rolled back to previous version."
    exit 1
fi
