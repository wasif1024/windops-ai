"""Read service configuration from the environment (DEP-003)."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_MINIMUM_SECRET_LENGTH = 32


class SettingsError(Exception):
    """A required setting is missing or invalid, so the service must not start."""


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise SettingsError(f"{name} must be an integer") from error


@dataclass(frozen=True)
class Settings:
    environment: str
    version: str
    identity_secret: str = field(repr=False)
    grpc_port: int = 50051
    http_port: int = 8080
    # Only used by a service that serves its HTTP API over TLS, such as Web BFF.
    https_port: int = 8443
    log_level: str = "INFO"
    otlp_endpoint: str | None = None
    shutdown_grace_seconds: int = 20
    # Where cert-manager's files are mounted. None means plain gRPC, which only from_env decides.
    tls_dir: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        secret = env.get("WINDOPS_IDENTITY_SECRET", "")
        if len(secret) < _MINIMUM_SECRET_LENGTH:
            raise SettingsError(
                f"WINDOPS_IDENTITY_SECRET must be at least {_MINIMUM_SECRET_LENGTH} characters"
            )
        environment = env.get("WINDOPS_ENVIRONMENT", "local")
        plaintext = environment == "test" and env.get("WINDOPS_PLAINTEXT_GRPC") == "true"
        tls_directory = env.get("WINDOPS_TLS_DIR")
        if not tls_directory and not plaintext:
            # GRP-005: no environment runs without mutual TLS by accident.
            raise SettingsError("WINDOPS_TLS_DIR must name this service's certificate directory")
        level = env.get("WINDOPS_LOG_LEVEL", "INFO").upper()
        if not isinstance(logging.getLevelNamesMapping().get(level), int):
            raise SettingsError("WINDOPS_LOG_LEVEL is not a valid level")
        return cls(
            environment=environment,
            version=env.get("WINDOPS_VERSION", "unknown"),
            identity_secret=secret,
            grpc_port=_integer(env, "WINDOPS_GRPC_PORT", 50051),
            http_port=_integer(env, "WINDOPS_HTTP_PORT", 8080),
            https_port=_integer(env, "WINDOPS_HTTPS_PORT", 8443),
            log_level=level,
            otlp_endpoint=env.get("WINDOPS_OTLP_ENDPOINT") or None,
            shutdown_grace_seconds=_integer(env, "WINDOPS_SHUTDOWN_GRACE_SECONDS", 20),
            tls_dir=Path(tls_directory) if tls_directory else None,
        )
