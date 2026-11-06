"""Start the Identity Service."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path

import grpc
from nats.errors import Error as NatsError
from nats.js.errors import APIError
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from windops.authorization.v1 import authorization_pb2_grpc
from windops.identity.v1 import identity_pb2_grpc
from windops_auth import current_identity_token
from windops_events import EventPublisher, connect, events_breaker
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import database_url
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_identity.keycloak import KeycloakAdmin, KeycloakSettings
from windops_identity.service import AuthorizationClient, IdentityService
from windops_identity.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def relay_audit_records(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish queued audit records. An outage only delays them, and must not stop the service."""
    client = None
    while client is None:
        if stop.is_set():
            return
        try:
            client = await connect(
                url=os.environ["WINDOPS_EVENTS_URL"],
                username=os.environ["WINDOPS_EVENTS_USERNAME"],
                password=os.environ["WINDOPS_EVENTS_PASSWORD"],
                name=SERVICE_NAME,
                ca_file=f"{os.environ['WINDOPS_TLS_DIR']}/ca.crt",
            )
        except (NatsError, APIError, TimeoutError, OSError) as error:
            logger.warning("Cannot reach JetStream yet", extra={"error_type": type(error).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    publisher = EventPublisher(client.jetstream(), producer=SERVICE_NAME, breaker=events_breaker())
    try:
        await OutboxRelay(engine, publisher).run_forever(stop)
    finally:
        await client.close()


def keycloak_settings() -> KeycloakSettings:
    return KeycloakSettings(
        url=os.environ["WINDOPS_KEYCLOAK_URL"],
        realm=os.environ.get("WINDOPS_KEYCLOAK_REALM", "windops"),
        client_id=os.environ["WINDOPS_KEYCLOAK_CLIENT_ID"],
        client_secret=os.environ["WINDOPS_KEYCLOAK_CLIENT_SECRET"],
        ca_file=f"{os.environ['WINDOPS_TLS_DIR']}/ca.crt",
        exchange_client_id=os.environ["WINDOPS_EXCHANGE_CLIENT_ID"],
        exchange_client_secret=os.environ["WINDOPS_EXCHANGE_CLIENT_SECRET"],
    )


def main() -> None:
    engine = create_async_engine(database_url(), pool_size=5, max_overflow=5, pool_pre_ping=True)
    keycloak = KeycloakAdmin(keycloak_settings())
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    address = os.environ["WINDOPS_AUTHORIZATION_ADDRESS"]
    state: dict[str, AuthorizationClient] = {}

    def authorizer() -> AuthorizationClient:
        # The channel must be made on the running event loop.
        if "client" not in state:
            stub = authorization_pb2_grpc.AuthorizationServiceStub(create_channel(address, tls))
            breaker = CircuitBreaker(
                "authorization",
                BreakerConfig(slow_call_seconds=1.0),
                on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
            )
            # The caller's own identity goes along, so Authorization decides for them.
            state["client"] = AuthorizationClient(
                stub, ServiceClient(breaker, identity_token=current_identity_token)
            )
        return state["client"]

    class LazyAuthorizer:
        async def allowed(self, permission: str, target_tenant_id: str) -> bool:
            return await authorizer().allowed(permission, target_tenant_id)

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        identity_pb2_grpc.add_IdentityServiceServicer_to_server(
            IdentityService(engine, keycloak, LazyAuthorizer()), server
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
