"""A client certificate for SASL EXTERNAL.

chonkline binds the SHA-256 of the leaf certificate's DER to an account and
checks neither a chain nor an expiry: "a self-signed leaf with an arbitrary
lifetime is the credential users actually have". So there is no CA to involve
and nothing to renew against -- a certificate is rotated, not renewed, because
reissuing changes the fingerprint and the fingerprint is the identity.

Generated with openssl rather than a library: this runs once per instance, and
a dependency carried year-round for a one-time setup step is a bad trade.
"""

from __future__ import annotations

import hashlib
import shutil
import ssl
import subprocess
from pathlib import Path

NAME = "chickenbot.pem"
DAYS = 3650  # not checked by the server; long enough not to be a trap either way
CURVE = "prime256v1"  # ECDSA P-256: what rustls's ring provider takes everywhere


def fingerprint(pem: str) -> str:
    """Lowercase hex SHA-256 of the leaf DER, as `CERT LIST` prints it."""
    start = pem.index("-----BEGIN CERTIFICATE-----")
    end = pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem[start:end] + "\n")).hexdigest()


def generate(path: Path, nick: str, days: int = DAYS) -> str:
    """Write key and certificate into one PEM at 0600. Returns the fingerprint."""
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl is not on PATH")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Created empty and locked down first: openssl would otherwise write the
    # private key at the umask's mercy for as long as it takes to chmod it.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    done = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "ec",
            "-pkeyopt",
            f"ec_paramgen_curve:{CURVE}",
            "-keyout",
            str(path),
            "-out",
            str(path),
            "-days",
            str(days),
            "-subj",
            f"/CN={nick}",
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        last = done.stderr.strip().splitlines()
        raise RuntimeError(last[-1] if last else "openssl failed")
    path.chmod(0o600)
    return fingerprint(path.read_text(encoding="utf-8"))
