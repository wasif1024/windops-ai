"""Recorded external providers for named tenants, live ones for everyone else (EVL-001, M12.6).

A regression runs in the evaluation tenant. Its web research and forecasts must be repeatable,
and may carry an injected instruction a scenario planted, so they come from the Evaluation
Service's recordings, never from Tavily or Open-Meteo. Every other tenant always gets the live
provider. Which tenants are replayed is a deployment's setting, set only where evaluations run:

- `WINDOPS_REPLAY_TENANTS`: the tenants, comma-separated. Unset, every call is live.
- `WINDOPS_REPLAY_URL`: the recordings' base, such as
  `https://windops-evaluation.windops-application.svc.cluster.local:8443/replay`.

The recordings are reached over mutual TLS, with the caller's own certificate from the cluster's
CA, so nothing outside the cluster can stand in for them.
"""

import os
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from windops_auth import IdentityError

from windops_grpc.identity import current_identity


@dataclass(frozen=True)
class ReplaySettings:
    tenants: frozenset[str]
    url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ReplaySettings | None":
        """The settings, or None when no tenant is replayed."""
        tenants = frozenset(t.strip() for t in env.get("WINDOPS_REPLAY_TENANTS", "").split(",")
                            if t.strip())  # fmt: skip
        if not tenants:
            return None
        url = env.get("WINDOPS_REPLAY_URL", "").rstrip("/")
        if not url.startswith("https://"):
            raise ValueError("WINDOPS_REPLAY_URL must be an https:// address")
        return cls(tenants, url)


def replay_ssl_context(tls_dir: Path) -> ssl.SSLContext:
    """Verifies the recordings against the cluster's CA, and presents this service's own
    certificate."""
    context = ssl.create_default_context(cafile=str(tls_dir / "ca.crt"))
    context.load_cert_chain(str(tls_dir / "tls.crt"), str(tls_dir / "tls.key"))
    return context


class ByTenant:
    """A provider that is the replayed one for the named tenants, and the live one otherwise,
    chosen on each call by the caller's tenant. With no caller, it is the live one."""

    def __init__(self, live: Any, replayed: Any, tenants: frozenset[str]) -> None:
        self._live, self._replayed, self._tenants = live, replayed, tenants

    def _chosen(self) -> Any:
        try:
            tenant = current_identity().tenant_id
        except IdentityError:
            return self._live
        return self._replayed if tenant in self._tenants else self._live

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chosen(), name)
