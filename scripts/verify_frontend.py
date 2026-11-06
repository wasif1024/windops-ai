#!/usr/bin/env python3
"""Sign in through a real browser, and see the branded, role-aware shell (SRS 4.11).

Run `make images`, `make services`, `make gateway-routes` and the seeds first. Needs local port
9443 free. The first run downloads Chromium for Playwright.
"""

import base64
import hashlib
import os
import ssl
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from identity_seed import demo_email, keycloak_public, read_demo_passwords
from tls_client import authority_file

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
TENANT = "north-sea"


def gateway_spki() -> str:
    """The base64 SHA-256 of the gateway certificate's public key, as Chromium pins it."""
    pem = ssl.get_server_certificate(("localhost", 9443))
    key = x509.load_pem_x509_certificate(pem.encode()).public_key()
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def main() -> int:
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=FRONTEND, check=True)
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=FRONTEND, check=True)
    with keycloak_public():
        env = {
            **{k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"},
            "BASE_URL": "https://localhost:9443",
            "GATEWAY_SPKI": gateway_spki(),
            "OPERATOR_EMAIL": demo_email(TENANT),
            "OPERATOR_PASSWORD": read_demo_passwords()[TENANT],
            "TENANT": TENANT,
            # Node's own requests trust the internal CA too, and nothing is turned off.
            "NODE_EXTRA_CA_CERTS": authority_file(),
        }
        result = subprocess.run(["npx", "playwright", "test"], cwd=FRONTEND, env=env, check=False)
    if result.returncode != 0:
        print("The browser check failed", file=sys.stderr)
        return 1
    print("Frontend check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
