#!/bin/sh
# Generate a persistent secret key on first start unless one is supplied.
# The key also encrypts stored camera passwords, so it must survive restarts.
set -e
mkdir -p /data
if [ -z "$ARGUS_SECRET_KEY" ] && ! grep -q '^ARGUS_SECRET_KEY=' /data/.env 2>/dev/null; then
    echo "ARGUS_SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" >> /data/.env
    chmod 600 /data/.env
    echo "argus: generated secret key in /data/.env (back this file up with your data)"
fi
exec python -m uvicorn sentinel.main:app --host "$ARGUS_HOST" --port "$ARGUS_PORT"
