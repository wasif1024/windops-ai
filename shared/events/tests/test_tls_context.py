"""Clients refuse TLS older than 1.2, even if a server offers it."""

import datetime
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from windops_events import tls_context


def authority(directory: Path) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = directory / "ca.crt"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return str(path)


def test_a_context_refuses_tls_older_than_1_2_and_checks_the_server(tmp_path: Path) -> None:
    context = tls_context(authority(tmp_path))
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    # Only the CA it was given, so the machine's usual roots cannot vouch for a store.
    assert [item["subject"] for item in context.get_ca_certs()] == [((("commonName", "test-ca"),),)]
