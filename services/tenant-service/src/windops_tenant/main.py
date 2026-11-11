"""Start the Tenant Service."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from windops.authorization.v1 import authorization_pb2_grpc
from windops.identity.v1 import identity_pb2_grpc
from windops.tenant.v1 import tenant_pb2_grpc
from windops_auth import current_identity_token
from windops_events import EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import database_url
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_tenant.service import AuthorizationClient, IdentityDirectory, TenantService
from windops_tenant.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def relay_audit_records(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish queued audit records. An outage only delays them, and must not stop the service."""
    events = None
    while events is None:
        if stop.is_set():
            return
        try:
            # NATS JetStream or Azure Service Bus, as WINDOPS_ENVIRONMENT chooses (ADR 0006).
            events = await open_events(SERVICE_NAME)
        except EventsUnavailableError as error:
            logger.warning(
                "Cannot reach the events back end yet", extra={"error_type": type(error).__name__}
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    try:
        await OutboxRelay(engine, events.publisher).run_forever(stop)
    finally:
        await events.close()


class _Lazy:
    """Makes its client on first use, because a channel must be made on the running loop."""

    def __init__(self, make: Any) -> None:
        self._make = make
        self._made: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._made is None:
            self._made = self._make()
        return getattr(self._made, name)


def _client(dependency: str) -> ServiceClient:
    breaker = CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=1.0),
        on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
    )
    # The caller's own identity goes along, so the other service decides for them.
    return ServiceClient(breaker, identity_token=current_identity_token)


def main() -> None:
    engine = create_async_engine(database_url(), pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    authorizer = _Lazy(
        lambda: AuthorizationClient(
            authorization_pb2_grpc.AuthorizationServiceStub(
                create_channel(os.environ["WINDOPS_AUTHORIZATION_ADDRESS"], tls)
            ),
            _client("authorization"),
        )
    )
    directory = _Lazy(
        lambda: IdentityDirectory(
            identity_pb2_grpc.IdentityServiceStub(
                create_channel(os.environ["WINDOPS_IDENTITY_ADDRESS"], tls)
            ),
            _client("identity"),
        )
    )

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        tenant_pb2_grpc.add_TenantServiceServicer_to_server(
            TenantService(engine, authorizer, directory), server
        )

    async def background(stop: asyncio.Event) -> None:
        await relay_audit_records(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            background=(background,),
        )
    )


if __name__ == "__main__":
    main()
