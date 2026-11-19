"""Mutual TLS for gRPC, using the certificate cert-manager mounts in each pod (GRP-005)."""

import asyncio
import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any

import grpc

logger = logging.getLogger("windops.service")

CERTIFICATE_FILE = "tls.crt"
PRIVATE_KEY_FILE = "tls.key"
AUTHORITY_FILE = "ca.crt"
WATCH_SECONDS = 30.0
# Every channel pings its server every 20 seconds, and gives up on a connection that does not
# answer within 10. Without this, a connection to a pod that has gone, as after a rollout, hangs
# every call until the operating system notices, minutes later, while the new pod waits unused.
CHANNEL_OPTIONS = (
    ("grpc.keepalive_time_ms", 20_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.max_pings_without_data", 0),
)
# What a server accepts, so those pings are never taken for abuse: otherwise it answers them with
# GOAWAY "too_many_pings" and drops the connection. gRPC ignores a name it does not know, so these
# are its exact names, which a test checks against the library itself.
SERVER_OPTIONS = (
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.http2.min_ping_interval_without_data_ms", 10_000),
)
_COMMON_NAME = "x509_common_name"


def _read(directory: Path) -> tuple[bytes, bytes, bytes]:
    return (
        (directory / CERTIFICATE_FILE).read_bytes(),
        (directory / PRIVATE_KEY_FILE).read_bytes(),
        (directory / AUTHORITY_FILE).read_bytes(),
    )


def server_credentials(directory: Path) -> grpc.ServerCredentials:
    """Return credentials that refuse any client without a certificate from this CA."""
    certificate, key, authority = _read(directory)
    return grpc.ssl_server_credentials(
        [(key, certificate)], root_certificates=authority, require_client_auth=True
    )


def channel_credentials(directory: Path) -> grpc.ChannelCredentials:
    """Return credentials that prove who this service is, and check who it is calling."""
    certificate, key, authority = _read(directory)
    return grpc.ssl_channel_credentials(
        root_certificates=authority, private_key=key, certificate_chain=certificate
    )


def create_channel(target: str, tls_directory: Path | None) -> grpc.aio.Channel:
    """Open a channel. Without a directory it is plain, which only tests can reach (Settings)."""
    if tls_directory is None:
        return grpc.aio.insecure_channel(target, options=CHANNEL_OPTIONS)
    return grpc.aio.secure_channel(
        target, channel_credentials(tls_directory), options=CHANNEL_OPTIONS
    )


def peer_service(context: Any) -> str | None:
    """Return the name in the caller's verified certificate, if it presented one."""
    values = context.auth_context().get(_COMMON_NAME)
    return values[0].decode() if values else None


def fingerprint(directory: Path) -> str:
    """Return a value that changes whenever the certificate files do."""
    digest = hashlib.sha256()
    for part in _read(directory):
        digest.update(part)
    return digest.hexdigest()


async def stop_when_certificate_changes(
    directory: Path, stop: asyncio.Event, *, interval_seconds: float = WATCH_SECONDS
) -> None:
    """Stop the service when its certificate is renewed, so Kubernetes restarts it with the new one.

    gRPC reads certificates once, so a renewed one would otherwise go unused until the old expired.
    """
    current = fingerprint(directory)
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        if stop.is_set():
            return
        try:
            latest = fingerprint(directory)
        except OSError as error:
            # Kubernetes swaps these files at once, but a read can still land mid-way.
            logger.warning(
                "Could not read the certificate, and will retry",
                extra={"error_type": type(error).__name__},
            )
            continue
        if latest != current:
            logger.info("The certificate was renewed, so the service will restart")
            stop.set()
