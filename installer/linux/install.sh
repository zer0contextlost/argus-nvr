#!/usr/bin/env bash
# Argus NVR — Linux installer
# Supports: Ubuntu 22.04/24.04, Debian 12/13
#
# Usage:
#   sudo ./install.sh              # fresh install
#   sudo ./install.sh --upgrade    # upgrade in-place (preserves data)

set -euo pipefail

INSTALL_ROOT="/opt/argus"
CONF_DIR="/etc/argus"
CONF_FILE="$CONF_DIR/.env"
SERVICE_NAME="argus"
SERVICE_FILE="/etc/systemd/system/argus.service"
UPDATE_SCRIPT="/usr/local/bin/argus-update"
ARGUS_USER="argus"
APP_DIR="$INSTALL_ROOT/app"
TOOLS_DIR="$INSTALL_ROOT/tools"
VENV="$APP_DIR/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UPGRADE=false
if [[ "${1:-}" == "--upgrade" ]]; then UPGRADE=true; fi

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root (sudo ./install.sh)" >&2
    exit 1
fi

if ! grep -qE "(ubuntu|debian)" /etc/os-release 2>/dev/null; then
    echo "WARNING: This installer is tested on Ubuntu 22.04/24.04 and Debian 12/13."
    read -rp "Continue anyway? [y/N] " ans
    [[ "${ans,,}" == "y" ]] || exit 1
fi

if [[ -d "$APP_DIR" && "$UPGRADE" != "true" ]]; then
    echo "ERROR: Argus is already installed at $APP_DIR."
    echo "  Run with --upgrade to update in-place, or uninstall first."
    exit 1
fi

echo "=== Argus NVR Installer ==="
echo "Install root: $INSTALL_ROOT"
[[ "$UPGRADE" == "true" ]] && echo "Mode: UPGRADE" || echo "Mode: FRESH INSTALL"
echo

# ---------------------------------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------------------------------
echo "[1/9] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3.11 python3.11-venv python3-pip libgl1 libglib2.0-0

# ---------------------------------------------------------------------------
# 2. Create argus system user
# ---------------------------------------------------------------------------
echo "[2/9] Creating argus user..."
if ! id "$ARGUS_USER" &>/dev/null; then
    useradd --system --home "$INSTALL_ROOT" --shell /usr/sbin/nologin "$ARGUS_USER"
    echo "  user '$ARGUS_USER' created"
else
    echo "  user '$ARGUS_USER' already exists"
fi

# ---------------------------------------------------------------------------
# 3. Stop service if upgrading
# ---------------------------------------------------------------------------
if [[ "$UPGRADE" == "true" ]]; then
    echo "[3/9] Stopping service for upgrade..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    if [[ -d "$APP_DIR" ]]; then
        mv "$APP_DIR" "${APP_DIR}.bak"
        echo "  backup: ${APP_DIR}.bak"
    fi
else
    echo "[3/9] Skipping service stop (fresh install)"
fi

# ---------------------------------------------------------------------------
# 4. Extract app and tools
# ---------------------------------------------------------------------------
echo "[4/9] Extracting application..."
mkdir -p "$APP_DIR" "$TOOLS_DIR"

PAYLOAD="$SCRIPT_DIR/payload"
if [[ -d "$PAYLOAD/app" ]]; then
    cp -a "$PAYLOAD/app/." "$APP_DIR/"
else
    # When run directly from the source repo (dev/test)
    REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
    cp -a "$REPO_ROOT/sentinel"         "$APP_DIR/"
    cp -a "$REPO_ROOT/frontend/dist"    "$APP_DIR/frontend/"
    cp    "$REPO_ROOT/requirements.txt" "$APP_DIR/"
    cp    "$REPO_ROOT/requirements-face.txt" "$APP_DIR/"
    cp -a "$REPO_ROOT/installer"        "$APP_DIR/"
fi

if [[ -d "$PAYLOAD/tools" ]]; then
    cp -a "$PAYLOAD/tools/." "$TOOLS_DIR/"
    chmod +x "$TOOLS_DIR/go2rtc" "$TOOLS_DIR/ffmpeg" "$TOOLS_DIR/ffprobe" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 5. Create Python venv and install dependencies
# ---------------------------------------------------------------------------
echo "[5/9] Creating Python virtual environment..."
python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade --quiet pip
echo "  installing dependencies (this may take a moment)..."
"$VENV/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
echo "  pip install complete"

# ---------------------------------------------------------------------------
# 6. Create runtime directories
# ---------------------------------------------------------------------------
echo "[6/9] Creating runtime directories..."
for d in data recordings events config logs; do
    mkdir -p "$INSTALL_ROOT/$d"
done

# ---------------------------------------------------------------------------
# 7. Generate /etc/argus/.env (skip if upgrading and it already exists)
# ---------------------------------------------------------------------------
echo "[7/9] Configuring environment..."
mkdir -p "$CONF_DIR"
chmod 750 "$CONF_DIR"

if [[ ! -f "$CONF_FILE" ]] || [[ "$UPGRADE" != "true" ]]; then
    "$VENV/bin/python" "$APP_DIR/installer/common/generate_env.py" \
        "$INSTALL_ROOT" "$CONF_FILE"
    chmod 640 "$CONF_FILE"
    echo "  .env written to $CONF_FILE"
else
    echo "  .env exists — preserved (upgrade)"
fi

# ---------------------------------------------------------------------------
# 8. Set ownership
# ---------------------------------------------------------------------------
echo "[8/9] Setting ownership..."
chown -R "$ARGUS_USER:$ARGUS_USER" "$INSTALL_ROOT"
chown -R "$ARGUS_USER:$ARGUS_USER" "$CONF_DIR"

# ---------------------------------------------------------------------------
# 9. Install systemd service and update script
# ---------------------------------------------------------------------------
echo "[9/9] Installing systemd service..."
cp "$SCRIPT_DIR/argus.service" "$SERVICE_FILE"
cp "$SCRIPT_DIR/argus-update.sh" "$UPDATE_SCRIPT"
chmod +x "$UPDATE_SCRIPT"

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

# Remove backup on success
if [[ "$UPGRADE" == "true" ]] && [[ -d "${APP_DIR}.bak" ]]; then
    rm -rf "${APP_DIR}.bak"
    echo "  upgrade backup removed"
fi

# Open firewall port if ufw is active
if command -v ufw &>/dev/null && ufw status | grep -q "^Status: active"; then
    ufw allow 8080/tcp comment "Argus NVR" 2>/dev/null || true
    echo "  ufw: port 8080/tcp opened"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "============================================================"
echo " Argus NVR installed successfully!"
echo
echo "  URL:      http://${LAN_IP:-localhost}:8080"
echo "  Username: admin"
echo "  Password: Argus123!  (you will be prompted to change it)"
echo
echo "  Logs:     journalctl -u argus -f"
echo "  Update:   sudo argus-update"
echo "============================================================"
