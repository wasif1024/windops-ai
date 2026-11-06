"""Reach a service's gRPC port from this machine, with a client certificate from the cluster."""

import base64
import functools
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass

import grpc
from settings import KUBE_CONTEXT

CONTEXT = KUBE_CONTEXT
NAMESPACE = "windops-application"
CERTIFICATE = "windops-check-client"
_MANIFEST = {
    "apiVersion": "cert-manager.io/v1",
    "kind": "Certificate",
    "metadata": {"name": CERTIFICATE, "namespace": NAMESPACE},
    "spec": {
        "secretName": CERTIFICATE,
        "commonName": CERTIFICATE,
        "duration": "24h",
        "renewBefore": "1h",
        "privateKey": {"algorithm": "ECDSA", "size": 256},
        "usages": ["client auth"],
        "issuerRef": {
            "name": "windops-internal",
            "kind": "ClusterIssuer",
            "group": "cert-manager.io",
        },
    },
}


@dataclass(frozen=True)
class ClientBundle:
    certificate: bytes
    key: bytes
    authority: bytes


def _kubectl(*arguments: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def client_bundle() -> ClientBundle:
    """Request the check's certificate, and wait for it. It lasts a day, and is not a service."""
    _kubectl("apply", "--filename=-", stdin=json.dumps(_MANIFEST))
    _kubectl(
        "wait", f"certificate/{CERTIFICATE}", f"--namespace={NAMESPACE}",
        "--for=condition=Ready", "--timeout=120s",
    )  # fmt: skip
    for _ in range(30):
        output = _kubectl(
            "get", "secret", CERTIFICATE, f"--namespace={NAMESPACE}", "--output=json",
            "--ignore-not-found",
        )  # fmt: skip
        if output.strip():
            data = json.loads(output)["data"]
            return ClientBundle(
                base64.b64decode(data["tls.crt"]),
                base64.b64decode(data["tls.key"]),
                base64.b64decode(data["ca.crt"]),
            )
        time.sleep(2)
    raise RuntimeError("The check's certificate never appeared")


@functools.lru_cache(maxsize=1)
def authority_file() -> str:
    """Write the internal CA's public certificate to a file, to check a data store with."""
    encoded = _kubectl(
        "get", "secret", "windops-internal-ca", "--namespace=cert-manager",
        "--output=jsonpath={.data.ca\\.crt}",
    )  # fmt: skip
    if not encoded:
        raise RuntimeError("The internal CA has no certificate yet")
    with tempfile.NamedTemporaryFile("wb", suffix=".crt", delete=False) as file:
        file.write(base64.b64decode(encoded))
    return file.name


def trust_cluster_ca() -> None:
    """Make every HTTPS client in this process trust the internal CA, and only it.

    httpx reads SSL_CERT_FILE, so checks need not pass a context to each call. verify_cluster.run
    removes it for the tools a check starts, which must still trust the public internet.
    """
    os.environ["SSL_CERT_FILE"] = authority_file()


def credentials(bundle: ClientBundle) -> grpc.ChannelCredentials:
    return grpc.ssl_channel_credentials(
        root_certificates=bundle.authority,
        private_key=bundle.key,
        certificate_chain=bundle.certificate,
    )


def _options(service: str) -> tuple[tuple[str, str], ...]:
    # The port-forward's address is not in the certificate, so name the service it leads to.
    return (("grpc.ssl_target_name_override", service),)


def blocking_channel(port: int, service: str, bundle: ClientBundle) -> grpc.Channel:
    return grpc.secure_channel(f"127.0.0.1:{port}", credentials(bundle), options=_options(service))


def async_channel(port: int, service: str, bundle: ClientBundle) -> grpc.aio.Channel:
    return grpc.aio.secure_channel(
        f"127.0.0.1:{port}", credentials(bundle), options=_options(service)
    )
