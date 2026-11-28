"""Each tenant's egress switches: the paths out of the platform it can turn off (EGR-002, ADR 0048).

The Tenant Service keeps them. Each service that sends data out checks the path first, where it
sends, and reports what stops while it is off, as it reports that path's outage (SRS 7.7).

- A path is on until the tenant turns it off.
- An answer is kept for `ttl_seconds`, so a busy service asks the Tenant Service rarely.
- When the Tenant Service cannot answer, the last answer stands. With none, the path counts as
  off: nothing leaves for a tenant whose choice is unknown.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from windops.tenant.v1 import tenant_pb2

from windops_grpc.client import ServiceClient
from windops_grpc.errors import DomainError
from windops_grpc.service_identity import acting_as_service


@dataclass(frozen=True)
class Path:
    name: str
    # Where it goes and what goes there (SRS 9.3).
    destination: str
    # What stops while it is off.
    when_off: str


CLAUDE = Path(
    "claude",
    "Anthropic API: the case evidence each agent needs",
    "Agents cannot investigate cases. Cases, evidence and deterministic checks carry on.",
)
EMBEDDINGS = Path(
    "embeddings",
    "Embedding provider: approved document passages and search questions",
    "Documents are not indexed and document search is paused, until it is on again.",
)
LANGSMITH_TRACES = Path(
    "langsmith_traces",
    "LangSmith: redacted traces of agent runs",
    "Agent runs are not traced in LangSmith. The audit trail is complete either way.",
)
LANGSMITH_SANDBOX = Path(
    "langsmith_sandbox",
    "LangSmith Sandbox: the minimum data for an approved analysis",
    "Agents cannot run analyses in the sandbox, and say so in their findings.",
)
FORECASTS = Path(
    "forecasts",
    "Forecast provider: a site's coordinates and a time window",
    "No new forecasts. Weather inputs are unavailable once the last one is stale, and decisions "
    "that need them fail closed.",
)
PATHS = (CLAUDE, EMBEDDINGS, LANGSMITH_TRACES, LANGSMITH_SANDBOX, FORECASTS)
BY_NAME = {path.name: path for path in PATHS}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Why not, for the data gap or unavailable feature the caller reports.
    reason: str = ""
    # False when the switches could not be read: nothing is sent, but the tenant has chosen
    # nothing, so a caller that waits and tries again should, rather than record it as turned off.
    known: bool = True


def turned_off(path: Path) -> str:
    return f"{path.destination.split(':')[0]} is turned off for this tenant (EGR-002)"


UNKNOWN = "The tenant's egress settings cannot be read, so nothing is sent (EGR-002)"
logger = logging.getLogger("windops.egress")
Read = Callable[[str], Awaitable[Mapping[str, bool]]]


class Gate(Protocol):
    async def check(self, tenant: str, path: Path) -> Decision: ...


class Open:
    """Every path on, for tests and for a service run without the Tenant Service."""

    async def check(self, tenant: str, path: Path) -> Decision:
        return Decision(True)


class Fixed:
    """The paths in `off` are turned off for every tenant, for tests and rehearsals."""

    def __init__(self, off: set[str] | None = None) -> None:
        self.off = off if off is not None else set()

    async def check(self, tenant: str, path: Path) -> Decision:
        return Decision(False, turned_off(path)) if path.name in self.off else Decision(True)


class Unreadable:
    """The Tenant Service has never answered: every path is off, as unknown, for tests."""

    async def check(self, tenant: str, path: Path) -> Decision:
        return Decision(False, UNKNOWN, known=False)


class EgressSwitches:
    """Whether a tenant lets a path send data out, kept briefly, and closed when unknown."""

    def __init__(
        self, read: Read, *, ttl_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._read, self._ttl, self._clock = read, ttl_seconds, clock
        self._known: dict[str, tuple[float, Mapping[str, bool]]] = {}

    async def check(self, tenant: str, path: Path) -> Decision:
        now = self._clock()
        kept = self._known.get(tenant)
        if kept is None or now - kept[0] >= self._ttl:
            try:
                kept = (now, await self._read(tenant))
                self._known[tenant] = kept
            except (DomainError, ConnectionError, TimeoutError) as error:
                logger.warning(
                    "Cannot read a tenant's egress switches",
                    extra={"tenant_id": tenant, "error_type": type(error).__name__,
                           "kept": kept is not None},
                )  # fmt: skip
                if kept is None:
                    return Decision(False, UNKNOWN, known=False)
        if kept[1].get(path.name, True):
            return Decision(True)
        return Decision(False, turned_off(path))


def tenant_reader(stub: object, client: ServiceClient, service: str, secret: str) -> Read:
    """Read a tenant's switches from the Tenant Service, as this service acting for the tenant."""

    async def read(tenant: str) -> Mapping[str, bool]:
        with acting_as_service(service, tenant, secret):
            answer: tenant_pb2.GetEgressResponse = await client.call(
                stub.GetEgress,  # type: ignore[attr-defined]
                tenant_pb2.GetEgressRequest(),
                idempotent=True,
                deadline_seconds=2.0,
            )
        return {p.path: p.enabled for p in answer.paths}

    return read


def egress_switches(
    stub: object, client: ServiceClient, service: str, secret: str
) -> EgressSwitches:
    """The switches as the Tenant Service keeps them, read as this service acting for a tenant."""
    return EgressSwitches(tenant_reader(stub, client, service, secret))
