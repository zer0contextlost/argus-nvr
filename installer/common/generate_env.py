"""
generate_env.py — write a .env file from env.template with a fresh secret key.

Usage:
  python generate_env.py <data_root> <output_env_path>

Example (Windows):
  python generate_env.py "C:\\Sentinel" "C:\\Sentinel\\.env"

Example (Linux):
  python generate_env.py /opt/sentinel /etc/sentinel/.env
"""

import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = Path(__file__).parent / "env.template"


def generate(data_root: str, output_path: str) -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    content = (
        template
        .replace("__SECRET_KEY__", secrets.token_urlsafe(32))
        .replace("__DATA_ROOT__", data_root)
        .replace("__INSTALL_DATE__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    )
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"Written: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python {sys.argv[0]} <data_root> <output_env_path>", file=sys.stderr)
        sys.exit(1)
    generate(sys.argv[1], sys.argv[2])
