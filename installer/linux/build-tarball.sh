#!/usr/bin/env bash
# build-tarball.sh — assemble the Linux installer tarball.
#
# Prerequisites:
#   - Node + npm available on PATH
#   - Python 3.11+ available
#   - Run build/download-binaries.py first to populate payload/tools/
#
# Usage:
#   ./installer/linux/build-tarball.sh
#   ./installer/linux/build-tarball.sh --version 1.0.1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERSION="1.0.0"
if [[ "${1:-}" == "--version" && -n "${2:-}" ]]; then VERSION="$2"; fi

STAGING="$REPO_ROOT/build/staging-linux"
DIST_DIR="$REPO_ROOT/dist"
TARBALL="$DIST_DIR/argus-$VERSION-linux-x86_64.tar.gz"

echo "=== Argus Linux Tarball Build v$VERSION ==="

# 1. Build frontend
echo "[1/4] Building frontend..."
bash "$REPO_ROOT/build/build-frontend.sh"

# 2. Download tool binaries if not already present
TOOLS_PAYLOAD="$SCRIPT_DIR/payload/tools"
if [[ ! -f "$TOOLS_PAYLOAD/go2rtc" ]]; then
    echo "[2/4] Downloading tool binaries..."
    python3 "$REPO_ROOT/build/download-binaries.py" --os linux --dest "$TOOLS_PAYLOAD"
else
    echo "[2/4] Tool binaries already present"
fi

# 3. Stage files
echo "[3/4] Staging..."
rm -rf "$STAGING"
mkdir -p "$STAGING/payload/app" "$STAGING/payload/tools"

# App source
cp -a "$REPO_ROOT/sentinel"              "$STAGING/payload/app/"
mkdir -p "$STAGING/payload/app/frontend"
cp -a "$REPO_ROOT/frontend/dist"         "$STAGING/payload/app/frontend/"
cp    "$REPO_ROOT/requirements.txt"      "$STAGING/payload/app/"
cp    "$REPO_ROOT/requirements-face.txt" "$STAGING/payload/app/"
cp -a "$REPO_ROOT/installer"             "$STAGING/payload/app/"
echo "$VERSION" > "$STAGING/payload/app/VERSION"

# Tool binaries
cp -a "$TOOLS_PAYLOAD/." "$STAGING/payload/tools/"

# Installer scripts
cp "$SCRIPT_DIR/install.sh"          "$STAGING/"
cp "$SCRIPT_DIR/uninstall.sh"        "$STAGING/"
cp "$SCRIPT_DIR/argus-update.sh"  "$STAGING/"
cp "$SCRIPT_DIR/argus.service"    "$STAGING/"
chmod +x "$STAGING/install.sh" "$STAGING/uninstall.sh" "$STAGING/argus-update.sh"

# 4. Pack
echo "[4/4] Creating tarball..."
mkdir -p "$DIST_DIR"
tar czf "$TARBALL" -C "$STAGING" .

# SHA-256 checksum
sha256sum "$TARBALL" > "$TARBALL.sha256"
echo "  checksum: $TARBALL.sha256"

# Cleanup staging
rm -rf "$STAGING"

echo
echo "Done:"
echo "  $TARBALL"
echo "  $TARBALL.sha256"
