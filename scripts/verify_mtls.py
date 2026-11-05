#!/usr/bin/env python3
"""Prove mutual TLS works on the real local cluster, including a real certificate renewal.

Run `make infra`, `make images` and `make services` first. This replaces the responder's
certificate, which restarts it. The system is left running and healthy.
"""

import base64
import datetime
import json
import ssl
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from gateway_routes import port_forward
from tls_client import ClientBundle, blocking_channel, client_bundle
from windops.sample.v1 import sample_pb2
from windops_grpc import VerifiedIdentity, sign_identity

CONTEXT = "windops"
APPLICATION = "windops-application"
SYSTEM = "windops-system"
RESPONDER = "windops-sample-responder"
HOST = f"{RESPONDER}.{APPLICATION}.svc.cluster.local"
SECRET_NAME = f"{RESPONDER}-tls"
DEPLOYMENT = f"deployment/{RESPONDER}"
SERVICES = ("windops-sample-responder", "windops-sample-caller", "windops-audit")
ECHO = "/windops.sample.v1.SampleResponderService/Echo"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def signing_key() -> str:
    encoded = kubectl(
        "get", "secret", "windops-internal-identity", f"--namespace={APPLICATION}",
        "--output=jsonpath={.data.signing-key}",
    )  # fmt: skip
    return base64.b64decode(encoded).decode()


def identity_metadata(key: str) -> tuple[tuple[str, str], ...]:
    token = sign_identity(VerifiedIdentity("svc-mtls-check", "tenant-mtls"), key, ttl_seconds=60)
    return (("x-windops-identity", token),)


def call(channel: grpc.Channel, key: str) -> sample_pb2.EchoResponse:
    method: Any = channel.unary_unary(
        ECHO,
        request_serializer=sample_pb2.EchoRequest.SerializeToString,
        response_deserializer=sample_pb2.EchoResponse.FromString,
    )
    response: sample_pb2.EchoResponse = method(
        sample_pb2.EchoRequest(message="hello"), timeout=5, metadata=identity_metadata(key)
    )
    return response


def code_of(channel: grpc.Channel, key: str) -> grpc.StatusCode:
    try:
        call(channel, key)
    except grpc.RpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    finally:
        channel.close()
    return grpc.StatusCode.OK


def rogue_credentials(authority: bytes) -> grpc.ChannelCredentials:
    """Return a client certificate that this cluster's CA never issued, and that trusts the CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, RESPONDER)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    return grpc.ssl_channel_credentials(
        root_certificates=authority,
        private_key=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        certificate_chain=certificate.public_bytes(serialization.Encoding.PEM),
    )


def check_certificates() -> None:
    for name in SERVICES:
        kubectl(
            "wait", f"certificate/{name}-tls", f"--namespace={APPLICATION}",
            "--for=condition=Ready", "--timeout=120s",
        )  # fmt: skip
    check(True, f"All {len(SERVICES)} services have a certificate from the internal CA (GRP-005)")
    for name in SERVICES:
        status = kubectl(
            "rollout",
            "status",
            f"deployment/{name}",
            f"--namespace={APPLICATION}",
            "--timeout=240s",
        )
        if "successfully rolled out" not in status:
            raise RuntimeError(f"{name} is not running: {status}")
    check(True, "They all start and pass their probes, with mTLS on")


def check_connections(port: int, bundle: ClientBundle, key: str) -> None:
    good = blocking_channel(port, HOST, bundle)
    reply = call(good, key)
    good.close()
    check(
        reply.tenant_id == "tenant-mtls",
        "A client with a certificate and a signed identity is answered",
    )
    plain = grpc.insecure_channel(f"127.0.0.1:{port}")
    check(code_of(plain, key) == grpc.StatusCode.UNAVAILABLE, "A plain connection is refused")
    anonymous = grpc.secure_channel(
        f"127.0.0.1:{port}",
        grpc.ssl_channel_credentials(root_certificates=bundle.authority),
        options=(("grpc.ssl_target_name_override", HOST),),
    )
    check(
        code_of(anonymous, key) == grpc.StatusCode.UNAVAILABLE,
        "TLS with no client certificate is refused",
    )
    rogue = grpc.secure_channel(
        f"127.0.0.1:{port}",
        rogue_credentials(bundle.authority),
        options=(("grpc.ssl_target_name_override", HOST),),
    )
    check(
        code_of(rogue, key) == grpc.StatusCode.UNAVAILABLE,
        "A client certificate from another authority, using a real service's name, is refused",
    )
    impostor = blocking_channel(port, "windops-audit.windops-application.svc.cluster.local", bundle)
    check(
        code_of(impostor, key) == grpc.StatusCode.UNAVAILABLE,
        "A client expecting another service refuses this one",
    )
    no_identity = blocking_channel(port, HOST, bundle)
    method: Any = no_identity.unary_unary(
        ECHO,
        request_serializer=sample_pb2.EchoRequest.SerializeToString,
        response_deserializer=sample_pb2.EchoResponse.FromString,
    )
    refused = None
    try:
        method(sample_pb2.EchoRequest(message="hello"), timeout=5)
    except grpc.RpcError as error:
        refused = error.code()
    no_identity.close()
    check(
        refused is grpc.StatusCode.UNAUTHENTICATED,
        "A certificate alone is not enough: a signed identity is still required (GRP-004)",
    )


def request_through_gateway(attempts: int = 60) -> dict[str, Any]:
    last: Any = None
    with port_forward("service/windops-gateway", SYSTEM, 80) as port:
        for _ in range(attempts):
            try:
                response = httpx.get(
                    f"http://127.0.0.1:{port}/sample/echo", params={"message": "hello"}, timeout=8
                )
                if response.status_code == 200:
                    body: dict[str, Any] = response.json()
                    return body
                last = response.status_code
            except httpx.HTTPError as error:
                last = type(error).__name__
            time.sleep(3)
    raise RuntimeError(f"The request never succeeded: {last}")


def current_certificate() -> str:
    secret = json.loads(
        kubectl("get", "secret", SECRET_NAME, f"--namespace={APPLICATION}", "--output=json")
    )
    return str(secret["data"]["tls.crt"])


def served_certificate(port: int) -> bytes:
    """Return the certificate the service presents, as a client sees it during the handshake."""
    served = ssl.get_server_certificate(("127.0.0.1", port))
    return ssl.PEM_cert_to_DER_cert(served)


def restart_count() -> int:
    output = kubectl(
        "get", "pods", f"--namespace={APPLICATION}",
        f"--selector=app.kubernetes.io/name={RESPONDER}",
        "--output=jsonpath={.items[*].status.containerStatuses[*].restartCount}",
    )  # fmt: skip
    return sum(int(item) for item in output.split())


def check_renewal() -> None:
    """Replace the certificate for real, and show the service picks it up by itself."""
    before = current_certificate()
    restarts = restart_count()
    kubectl("delete", "secret", SECRET_NAME, f"--namespace={APPLICATION}")
    for _ in range(60):
        try:
            if current_certificate() != before:
                break
        except RuntimeError:
            pass
        time.sleep(2)
    else:
        raise RuntimeError("Failed: cert-manager did not issue a new certificate")
    check(True, "cert-manager issues a new certificate when the old one is gone")
    for _ in range(90):
        if restart_count() > restarts:
            break
        time.sleep(4)
    else:
        raise RuntimeError("Failed: the service did not pick up its new certificate")
    check(True, "The service notices the new certificate, and restarts cleanly to use it")
    last_state = kubectl(
        "get", "pods", f"--namespace={APPLICATION}",
        f"--selector=app.kubernetes.io/name={RESPONDER}",
        "--output=jsonpath={.items[0].status.containerStatuses[0].lastState.terminated.exitCode}",
    )  # fmt: skip
    check(last_state == "0", "It exited cleanly, not by crashing")
    kubectl("rollout", "status", DEPLOYMENT, f"--namespace={APPLICATION}", "--timeout=240s")
    body = request_through_gateway()
    check(body.get("message") == "hello", "Calls work again after the restart")
    issued = base64.b64decode(current_certificate()).decode()
    with port_forward(DEPLOYMENT, APPLICATION, 50051) as port:
        served = served_certificate(port)
    check(
        served == ssl.PEM_cert_to_DER_cert(issued),
        "The service presents the new certificate, not the old one",
    )


def main() -> int:
    try:
        check_certificates()
        bundle = client_bundle()
        key = signing_key()
        with port_forward(DEPLOYMENT, APPLICATION, 50051) as port:
            check_connections(port, bundle, key)
        body = request_through_gateway()
        check(
            body == {"message": "hello", "tenant_id": "sample"},
            "A real service reaches another over mTLS, through the gateway",
        )
        logs = kubectl("logs", DEPLOYMENT, f"--namespace={APPLICATION}", "--tail=200")
        check(
            '"peer_service": "windops-sample-caller"' in logs,
            "The responder logged which service called it, from its certificate",
        )
        check_renewal()
    except (RuntimeError, OSError, httpx.HTTPError, grpc.RpcError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("mTLS check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
