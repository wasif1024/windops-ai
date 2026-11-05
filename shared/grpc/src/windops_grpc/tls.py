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
        return grpc.aio.insecure_channel(target)
    return grpc.aio.secure_channel(target, channel_credentials(tls_directory))


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
