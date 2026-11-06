#!/usr/bin/env python3
"""Prove every data store accepts only TLS, on the real local cluster, including a real renewal.

Run `make infra`, `make migrate` and `make services` first. This replaces each store's certificate,
and shows the store loads the new one with no restart. Everything is left running and healthy.
"""

import asyncio
import base64
import datetime
import json
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import grpc
import httpx
import psycopg
import redis.asyncio as redis
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from windops_cache import connect as redis_connect
from windops_events import connect as nats_connect
from windops_events import tls_context as verified_context

from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import authority_file
from scripts.setup.gateway_routes import port_forward

CONTEXT = KUBE_CONTEXT
DATA = "windops-data"
HOST = "127.0.0.1"
ADMIN = "windops_admin"
# Asking PostgreSQL to start TLS, from its protocol.
POSTGRES_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
# Long enough for a store's next look at its files, plus cert-manager and the kubelet.
RENEWAL_ATTEMPTS = 60
RENEWAL_WAIT_SECONDS = 5


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={DATA}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def secret_value(name: str, key: str) -> str:
    encoded = kubectl("get", "secret", name, f"--output=jsonpath={{.data.{key}}}")
    return base64.b64decode(encoded).decode()


def rogue_authority() -> str:
    """Write a CA that signed nothing here, so a client trusting only it must refuse the store."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rogue-ca")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    with tempfile.NamedTemporaryFile("wb", suffix=".crt", delete=False) as file:
        file.write(certificate.public_bytes(serialization.Encoding.PEM))
    return file.name


def tls_context() -> ssl.SSLContext:
    return verified_context(authority_file())


# Each returns the certificate a client sees, as DER, checked against our CA and this address.
def _served(sock: socket.socket) -> bytes:
    with tls_context().wrap_socket(sock, server_hostname=HOST) as wrapped:
        certificate = wrapped.getpeercert(binary_form=True)
    if certificate is None:
        raise RuntimeError("The store presented no certificate")
    return certificate


def served_directly(port: int) -> bytes:
    return _served(socket.create_connection((HOST, port), timeout=10))


def served_by_postgres(port: int) -> bytes:
    sock = socket.create_connection((HOST, port), timeout=10)
    sock.sendall(POSTGRES_SSL_REQUEST)
    if sock.recv(1) != b"S":
        sock.close()
        raise RuntimeError("PostgreSQL did not agree to TLS")
    return _served(sock)


def served_by_nats(port: int) -> bytes:
    sock = socket.create_connection((HOST, port), timeout=10)
    sock.recv(4096)  # NATS speaks first, with plain INFO, and TLS starts after that.
    return _served(sock)


def check_postgres(port: int) -> None:
    password = secret_value("windops-postgres-admin", "password")
    login: dict[str, Any] = {
        "host": HOST,
        "port": port,
        "user": ADMIN,
        "password": password,
        "dbname": "postgres",
    }
    refused = False
    try:
        psycopg.connect(**login, sslmode="disable", connect_timeout=5).close()
    except psycopg.OperationalError:
        refused = True
    check(refused, "PostgreSQL refuses a plain login, even with the right password")
    refused = False
    try:
        psycopg.connect(
            **login, sslmode="verify-full", sslrootcert=rogue_authority(), connect_timeout=5
        ).close()
    except psycopg.OperationalError:
        refused = True
    check(refused, "A PostgreSQL client that trusts another CA refuses the server")
    with psycopg.connect(
        **login, sslmode="verify-full", sslrootcert=authority_file(), connect_timeout=5
    ) as connection:
        row = connection.execute(
            "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        ).fetchone()
        check(row == (True,), "PostgreSQL accepts a login that checks its certificate")
        # What the server sees from every real client, which is harder to get wrong than a setting.
        rows = connection.execute(
            "SELECT a.usename, s.ssl FROM pg_stat_activity a JOIN pg_stat_ssl s USING (pid) "
            "WHERE a.client_addr IS NOT NULL"
        ).fetchall()
    plain = {str(user) for user, encrypted in rows if not encrypted}
    encrypted = {str(user) for user, encrypted in rows if encrypted}
    check(not plain, f"No network connection to PostgreSQL is unencrypted ({plain or 'none'})")
    check(
        {"windops_keycloak", "windops_audit"} <= encrypted,
        f"Keycloak and the Audit Service reach PostgreSQL over TLS ({sorted(encrypted)})",
    )


async def check_redis(port: int) -> None:
    password = secret_value("windops-redis-admin", "password")
    plain = redis.Redis(host=HOST, port=port, username=ADMIN, password=password, socket_timeout=5)
    refused = False
    try:
        await plain.ping()
    except (redis.ConnectionError, redis.TimeoutError):
        refused = True
    finally:
        await plain.aclose()
    check(refused, "Redis refuses a plain connection, even with the right password")
    wrong = redis_connect(
        host=HOST, port=port, username=ADMIN, password=password, ca_file=rogue_authority()
    )
    refused = False
    try:
        await wrong.ping()
    except redis.ConnectionError:
        refused = True
    finally:
        await wrong.aclose()
    check(refused, "A Redis client that trusts another CA refuses the server")
    good = redis_connect(
        host=HOST, port=port, username=ADMIN, password=password, ca_file=authority_file()
    )
    try:
        check(await good.ping() is True, "Redis accepts a client that checks its certificate")
    finally:
        await good.aclose()


def check_nats_plain(port: int, password: str) -> None:
    """Speak plain NATS by hand, so this shows the server refusing, not a careful client."""
    with socket.create_connection((HOST, port), timeout=10) as sock:
        info = sock.recv(4096).decode()
        check(
            json.loads(info.removeprefix("INFO "))["tls_required"] is True,
            "NATS announces that it requires TLS",
        )
        login = json.dumps({"user": ADMIN, "pass": password, "verbose": False})
        sock.sendall(f"CONNECT {login}\r\nPING\r\n".encode())
        reply = b""
        closed_by_server = True
        try:
            while chunk := sock.recv(4096):
                reply += chunk
        except OSError:
            closed_by_server = False
    # NATS expects a TLS handshake here, so it hangs up without reading the login.
    check(
        not reply and closed_by_server,
        "NATS refuses a plain login, even with the right password",
    )


async def check_nats(port: int) -> None:
    password = secret_value("windops-nats-admin", "password")
    check_nats_plain(port, password)
    url = f"tls://{HOST}:{port}"
    refused = False
    try:
        await asyncio.wait_for(
            _nats_once(url, password, rogue_authority()),
            timeout=8,
        )
    except (OSError, TimeoutError, ssl.SSLError):
        refused = True
    check(refused, "A NATS client that trusts another CA refuses the server")
    client = await nats_connect(
        url=url, username=ADMIN, password=password, name="data-tls-check", ca_file=authority_file()
    )
    try:
        check(client.is_connected, "NATS accepts a client that checks its certificate")
        await client.jetstream().stream_info("WINDOPS_EVENTS")
        check(True, "JetStream works over TLS")
    finally:
        await client.close()


async def _nats_once(url: str, password: str, ca_file: str) -> None:
    client = await nats_connect(
        url=url, username=ADMIN, password=password, name="data-tls-check", ca_file=ca_file
    )
    await client.close()


def check_qdrant_http(port: int) -> None:
    key = secret_value("windops-qdrant-api-key", "api-key")
    url = f"{HOST}:{port}/collections"
    refused = False
    try:
        httpx.get(f"http://{url}", headers={"api-key": key}, timeout=5).raise_for_status()
    except httpx.HTTPError:
        refused = True
    check(refused, "Qdrant's HTTP port refuses plain HTTP, even with the right key")
    refused = False
    try:
        wrong = verified_context(rogue_authority())
        httpx.get(f"https://{url}", headers={"api-key": key}, timeout=5, verify=wrong)
    except httpx.ConnectError:
        refused = True
    check(refused, "A Qdrant client that trusts another CA refuses the server")
    response = httpx.get(
        f"https://{url}", headers={"api-key": key}, timeout=10, verify=tls_context()
    )
    check(response.status_code == 200, "Qdrant's HTTP port accepts a client that checks it")


def qdrant_grpc_code(channel: grpc.Channel) -> grpc.StatusCode | None:
    call: grpc.UnaryUnaryMultiCallable[bytes, bytes] = channel.unary_unary(
        "/qdrant.Collections/List"
    )
    try:
        call(b"", timeout=10)
    except grpc.RpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return None


def check_qdrant_grpc(port: int) -> None:
    target = f"{HOST}:{port}"
    with grpc.insecure_channel(target) as channel:
        check(
            qdrant_grpc_code(channel) is grpc.StatusCode.UNAVAILABLE,
            "Qdrant's gRPC port refuses a plain connection",
        )
    authority = Path(authority_file()).read_bytes()
    with grpc.secure_channel(target, grpc.ssl_channel_credentials(authority)) as channel:
        # Refused for its missing key, which shows the TLS connection itself succeeded.
        check(
            qdrant_grpc_code(channel) is grpc.StatusCode.UNAUTHENTICATED,
            "Qdrant's gRPC port accepts TLS, and still needs its API key",
        )


def container_starts(selector: str) -> list[tuple[str, int]]:
    """Return each pod and its restart count, which any restart would change."""
    pods = json.loads(kubectl("get", "pods", f"--selector={selector}", "--output=json"))
    return [
        (pod["metadata"]["uid"], status["restartCount"])
        for pod in pods["items"]
        for status in pod["status"]["containerStatuses"]
    ]


def issued_certificate(secret: str) -> bytes | None:
    encoded = kubectl(
        "get", "secret", secret, "--ignore-not-found", "--output=jsonpath={.data.tls\\.crt}"
    )
    if not encoded:
        return None
    return ssl.PEM_cert_to_DER_cert(base64.b64decode(encoded).decode())


def last_exit_code(selector: str) -> str:
    return kubectl(
        "get", "pods", f"--selector={selector}",
        "--output=jsonpath={.items[0].status.containerStatuses[0].lastState.terminated.exitCode}",
    )  # fmt: skip


def check_renewal(
    store: str,
    target: str,
    ports: tuple[int, ...],
    served: Callable[[int], bytes],
    *,
    restarts: bool = False,
) -> None:
    """Replace the certificate for real, and show the store picks it up by itself."""
    name = f"windops-{store}"
    secret = f"{name}-tls"
    selector = f"app.kubernetes.io/name={name}"
    starts = container_starts(selector)
    before = issued_certificate(secret)
    kubectl("delete", "secret", secret)
    new = None
    for _ in range(RENEWAL_ATTEMPTS):
        new = issued_certificate(secret)
        if new is not None and new != before:
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"Failed: cert-manager did not reissue {secret}")
    started = time.monotonic()
    for port_number in ports:
        last: str = "the old certificate"
        for _ in range(RENEWAL_ATTEMPTS):
            try:
                with port_forward(target, DATA, port_number) as port:
                    if served(port) == new:
                        break
                last = "the old certificate"
            # A store that is restarting cannot be reached for a moment.
            except (OSError, RuntimeError, ssl.SSLError) as error:
                last = f"{type(error).__name__}: {error}"
            time.sleep(RENEWAL_WAIT_SECONDS)
        else:
            raise RuntimeError(f"Failed: {store} on {port_number} still shows {last}")
    seconds = round(time.monotonic() - started)
    check(True, f"{store} presents its renewed certificate on every port, after {seconds}s")
    if restarts:
        kubectl("wait", f"{target}", "--for=condition=Ready", "--timeout=180s")
        check(container_starts(selector) != starts, f"{store} restarted to load it")
        check(last_exit_code(selector) == "0", f"{store} stopped cleanly, not by crashing")
    else:
        check(container_starts(selector) == starts, f"{store} did this without restarting")


async def run() -> None:
    with port_forward("pod/windops-postgres-0", DATA, 5432) as port:
        check_postgres(port)
    with port_forward("deployment/windops-redis", DATA, 6379) as port:
        await check_redis(port)
    with port_forward("pod/windops-nats-0", DATA, 4222) as port:
        await check_nats(port)
    with port_forward("pod/windops-qdrant-0", DATA, 6333) as port:
        check_qdrant_http(port)
    with port_forward("pod/windops-qdrant-0", DATA, 6334) as port:
        check_qdrant_grpc(port)
    check_renewal("postgres", "pod/windops-postgres-0", (5432,), served_by_postgres)
    check_renewal("redis", "deployment/windops-redis", (6379,), served_directly)
    check_renewal("nats", "pod/windops-nats-0", (4222,), served_by_nats)
    check_renewal("qdrant", "pod/windops-qdrant-0", (6333, 6334), served_directly, restarts=True)
    # The same checks again, because a reload that broke a store would show only now.
    with port_forward("pod/windops-postgres-0", DATA, 5432) as port:
        check_postgres(port)
    with port_forward("deployment/windops-redis", DATA, 6379) as port:
        await check_redis(port)
    with port_forward("pod/windops-nats-0", DATA, 4222) as port:
        await check_nats(port)
    with port_forward("pod/windops-qdrant-0", DATA, 6333) as port:
        check_qdrant_http(port)
    with port_forward("pod/windops-qdrant-0", DATA, 6334) as port:
        check_qdrant_grpc(port)


def main() -> int:
    try:
        asyncio.run(run())
    except (RuntimeError, OSError, httpx.HTTPError, grpc.RpcError, psycopg.Error) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Data store TLS check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
