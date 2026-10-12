#!/usr/bin/env bash
# Argus NVR installer / upgrader for Debian 12+, Ubuntu 24.04+ (x86_64, arm64).
#
#   curl -fsSL https://raw.githubusercontent.com/zer0contextlost/argus-nvr/main/install.sh | sudo bash
#
# Running it again upgrades in place. Recordings, the database and
# /etc/argus/.env are never touched by an upgrade.
#
# Environment overrides:
#   ARGUS_VERSION=v1.2.0      install a specific release instead of the latest
#   ARGUS_TARBALL=/path.tgz   install from a local release tarball
#   ARGUS_PORT=8080           web port written to a fresh .env
#   ARGUS_ADMIN_PASSWORD=...  initial admin password (fresh install only)
#
# Run from a git checkout (sudo ./install.sh) to install that checkout; the
# frontend is built with npm if frontend/dist is missing.

set -euo pipefail

REPO="zer0contextlost/argus-nvr"
GO2RTC_VERSION="v1.9.14"

INSTALL_ROOT="/opt/argus"
APP_DIR="$INSTALL_ROOT/app"
TOOLS_DIR="$INSTALL_ROOT/tools"
CONF_DIR="/etc/argus"
CONF_FILE="$CONF_DIR/.env"
SERVICE_NAME="argus"
ARGUS_USER="argus"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo bash install.sh)"
command -v apt-get >/dev/null || die "this installer needs a Debian/Ubuntu system (apt). On other systems use Docker: see README."
command -v systemctl >/dev/null || die "systemd is required"

case "$(uname -m)" in
    x86_64|amd64)  ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) die "unsupported CPU architecture: $(uname -m)" ;;
esac

UPGRADE=false
[[ -d "$APP_DIR/sentinel" ]] && UPGRADE=true

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv ffmpeg curl ca-certificates logrotate libgl1 libglib2.0-0 >/dev/null

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || die "Python 3.11+ required, found $(python3 -V 2>&1). Use Debian 12+/Ubuntu 24.04+, or Docker."

# ---------------------------------------------------------------------------
# Locate the app source: local checkout, local tarball, or GitHub release.
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [[ -n "${ARGUS_TARBALL:-}" ]]; then
    say "Using local tarball $ARGUS_TARBALL"
    tar xzf "$ARGUS_TARBALL" -C "$WORK"
    SRC="$WORK/argus-nvr"
elif [[ -n "$SCRIPT_DIR" && -d "$SCRIPT_DIR/sentinel" ]]; then
    say "Using source checkout at $SCRIPT_DIR"
    SRC="$SCRIPT_DIR"
    if [[ ! -f "$SRC/frontend/dist/index.html" ]]; then
        command -v npm >/dev/null || die "frontend/dist is missing and npm isn't installed. Install Node.js 20+, or use the release installer (curl ... | sudo bash)."
        say "Building frontend"
        (cd "$SRC/frontend" && npm ci --no-audit --no-fund && npm run build)
    fi
else
    if [[ -n "${ARGUS_VERSION:-}" ]]; then
        URL="https://github.com/$REPO/releases/download/$ARGUS_VERSION/argus-nvr.tar.gz"
    else
        URL="https://github.com/$REPO/releases/latest/download/argus-nvr.tar.gz"
    fi
    say "Downloading $URL"
    curl -fsSL -o "$WORK/argus-nvr.tar.gz" "$URL" || die "download failed"
    tar xzf "$WORK/argus-nvr.tar.gz" -C "$WORK"
    SRC="$WORK/argus-nvr"
fi
[[ -d "$SRC/sentinel" && -f "$SRC/frontend/dist/index.html" ]] || die "app source at $SRC is incomplete"

# ---------------------------------------------------------------------------
if ! id "$ARGUS_USER" &>/dev/null; then
    say "Creating system user $ARGUS_USER"
    useradd --system --home "$INSTALL_ROOT" --shell /usr/sbin/nologin "$ARGUS_USER"
fi

if $UPGRADE; then
    say "Upgrading existing install (data and config are kept)"
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    rm -rf "$APP_DIR.bak"
    mv "$APP_DIR" "$APP_DIR.bak"
else
    say "Installing to $INSTALL_ROOT"
fi

rollback() {
    if $UPGRADE && [[ -d "$APP_DIR.bak" ]]; then
        echo "Install failed, restoring previous version" >&2
        [[ -d "$APP_DIR/venv" && ! -d "$APP_DIR.bak/venv" ]] && mv "$APP_DIR/venv" "$APP_DIR.bak/venv"
        rm -rf "$APP_DIR"
        mv "$APP_DIR.bak" "$APP_DIR"
        systemctl start "$SERVICE_NAME" 2>/dev/null || true
    fi
}
trap 'rollback; rm -rf "$WORK"' ERR

mkdir -p "$APP_DIR/frontend" "$TOOLS_DIR"
cp -a "$SRC/sentinel" "$APP_DIR/"
cp -a "$SRC/frontend/dist" "$APP_DIR/frontend/"
cp -a "$SRC/installer" "$APP_DIR/"
cp "$SRC/requirements.txt" "$SRC/requirements-face.txt" "$APP_DIR/"
cp "$SRC/install.sh" "$APP_DIR/"
[[ -f "$SRC/VERSION" ]] && cp "$SRC/VERSION" "$APP_DIR/"
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +

say "Installing Python dependencies (a few minutes on first install)"
if $UPGRADE && [[ -x "$APP_DIR.bak/venv/bin/python" ]]; then
    mv "$APP_DIR.bak/venv" "$APP_DIR/venv"
else
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------------------
GO2RTC_BIN="$TOOLS_DIR/go2rtc"
if [[ ! -x "$GO2RTC_BIN" ]] || ! "$GO2RTC_BIN" -version 2>/dev/null | grep -q "${GO2RTC_VERSION#v}"; then
    say "Downloading go2rtc $GO2RTC_VERSION ($ARCH)"
    curl -fsSL -o "$GO2RTC_BIN.new" \
        "https://github.com/AlexxIT/go2rtc/releases/download/$GO2RTC_VERSION/go2rtc_linux_$ARCH"
    chmod +x "$GO2RTC_BIN.new"
    mv "$GO2RTC_BIN.new" "$GO2RTC_BIN"
fi

# ---------------------------------------------------------------------------
for d in data recordings events logs; do mkdir -p "$INSTALL_ROOT/$d"; done
mkdir -p "$CONF_DIR"
chmod 750 "$CONF_DIR"
if [[ -f "$CONF_FILE" ]]; then
    # Never regenerate: a new secret key would make stored camera passwords unreadable.
    say "Keeping existing $CONF_FILE"
else
    say "Writing $CONF_FILE with a new secret key"
    "$APP_DIR/venv/bin/python" "$APP_DIR/installer/common/generate_env.py" "$INSTALL_ROOT" "$CONF_FILE"
    sed -i "s/^ARGUS_PORT=.*/ARGUS_PORT=${ARGUS_PORT:-8080}/" "$CONF_FILE"
    if [[ -n "${ARGUS_ADMIN_PASSWORD:-}" ]]; then
        printf 'ARGUS_ADMIN_PASSWORD=%s\n' "$ARGUS_ADMIN_PASSWORD" >> "$CONF_FILE"
    fi
fi
chmod 640 "$CONF_FILE"
chown -R "$ARGUS_USER:$ARGUS_USER" "$INSTALL_ROOT" "$CONF_DIR"

install -m 644 "$APP_DIR/installer/linux/argus.service" /etc/systemd/system/argus.service
install -m 644 "$APP_DIR/installer/linux/argus.logrotate" /etc/logrotate.d/argus
cat > /usr/local/bin/argus-update <<'EOF'
#!/usr/bin/env bash
# Upgrade Argus NVR to the latest release (or ARGUS_VERSION=vX.Y.Z).
set -euo pipefail
curl -fsSL https://raw.githubusercontent.com/zer0contextlost/argus-nvr/main/install.sh | bash
EOF
chmod 755 /usr/local/bin/argus-update

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

if command -v ufw &>/dev/null && ufw status | grep -q "^Status: active"; then
    PORT="$(sed -n 's/^ARGUS_PORT=//p' "$CONF_FILE")"
    ufw allow "${PORT:-8080}/tcp" comment "Argus NVR" >/dev/null || true
    ufw allow 8555 comment "Argus NVR WebRTC" >/dev/null || true
fi

# ---------------------------------------------------------------------------
say "Waiting for Argus to come up"
PORT="$(sed -n 's/^ARGUS_PORT=//p' "$CONF_FILE")"; PORT="${PORT:-8080}"
for _ in $(seq 1 60); do
    if curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
        rm -rf "$APP_DIR.bak"
        trap - ERR
        # The admin user exists now; don't leave the initial password on disk.
        sed -i '/^ARGUS_ADMIN_PASSWORD=/d' "$CONF_FILE"
        LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
        echo
        echo "  Argus NVR is running: http://${LAN_IP:-localhost}:$PORT"
        if ! $UPGRADE; then
            if [[ -n "${ARGUS_ADMIN_PASSWORD:-}" ]]; then
                echo "  Login: admin / (the ARGUS_ADMIN_PASSWORD you set)"
            else
                echo "  Login: admin / Screenshot123!  (you'll be asked to change it)"
            fi
        fi
        echo "  Logs:    journalctl -u argus -f   or   /opt/argus/logs/"
        echo "  Upgrade: sudo argus-update"
        exit 0
    fi
    sleep 2
done
echo "Argus didn't answer on port $PORT within 2 minutes." >&2
echo "Check /opt/argus/logs/stderr.log and 'systemctl status argus'." >&2
false
