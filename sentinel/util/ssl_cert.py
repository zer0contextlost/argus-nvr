"""Generate a self-signed TLS certificate if none exists."""
from __future__ import annotations

import datetime
import ipaddress
import socket
from pathlib import Path

from sentinel.util.logging import get_logger

log = get_logger("sentinel.ssl")


def ensure_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Generate self-signed cert+key at the given paths if they don't exist."""
    if cert_path.exists() and key_path.exists():
        log.info("ssl: cert already exists at %s", cert_path)
        return

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        raise RuntimeError(
            "pip install cryptography is required for HTTPS support"
        )

    log.info("ssl: generating self-signed certificate at %s", cert_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    hostname = socket.gethostname()
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sentinel NVR"),
    ])

    sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(hostname),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    try:
        ip = socket.gethostbyname(hostname)
        if ip != "127.0.0.1":
            sans.append(x509.IPAddress(ipaddress.IPv4Address(ip)))
    except Exception:
        pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    log.info("ssl: certificate generated (10 year validity)")
