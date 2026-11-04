"""Read service configuration from the environment (DEP-003)."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

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
    log_level: str = "INFO"
    otlp_endpoint: str | None = None
    shutdown_grace_seconds: int = 20

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        secret = env.get("WINDOPS_IDENTITY_SECRET", "")
        if len(secret) < _MINIMUM_SECRET_LENGTH:
            raise SettingsError(
                f"WINDOPS_IDENTITY_SECRET must be at least {_MINIMUM_SECRET_LENGTH} characters"
            )
        level = env.get("WINDOPS_LOG_LEVEL", "INFO").upper()
        if not isinstance(logging.getLevelNamesMapping().get(level), int):
            raise SettingsError("WINDOPS_LOG_LEVEL is not a valid level")
        return cls(
            environment=env.get("WINDOPS_ENVIRONMENT", "local"),
            version=env.get("WINDOPS_VERSION", "unknown"),
            identity_secret=secret,
            grpc_port=_integer(env, "WINDOPS_GRPC_PORT", 50051),
            http_port=_integer(env, "WINDOPS_HTTP_PORT", 8080),
            log_level=level,
            otlp_endpoint=env.get("WINDOPS_OTLP_ENDPOINT") or None,
            shutdown_grace_seconds=_integer(env, "WINDOPS_SHUTDOWN_GRACE_SECONDS", 20),
        )
