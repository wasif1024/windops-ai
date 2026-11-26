"""Start the Evaluation Service: its API, and the relay that sends its audit records."""

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.authorization.v1 import authorization_pb2_grpc
from windops.evaluation.v1 import evaluation_pb2_grpc
from windops_auth import current_identity_token
from windops_events import Events, EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_evaluation.authorizer import AuthorizationClient
from windops_evaluation.service import EvaluationService
from windops_evaluation.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


async def relay(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish the outbox: audit requests. An outage must not restart this service (RES-006), so
    it keeps trying, and the records wait in the outbox."""
    events: Events | None = None
    while events is None:
        if stop.is_set():
            return
        try:
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


def main() -> None:
    engine = create_service_engine(pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    channels: dict[str, Any] = {}

    def authorization() -> Any:
        if "authorization" not in channels:
            channels["authorization"] = create_channel(
                os.environ["WINDOPS_AUTHORIZATION_ADDRESS"], tls
            )
        return authorization_pb2_grpc.AuthorizationServiceStub(channels["authorization"])

    breaker = CircuitBreaker(
        "authorization",
        BreakerConfig(slow_call_seconds=1.0),
        on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
    )
    # The caller's own identity goes along, so Authorization decides for them.
    authorizer = AuthorizationClient(
        authorization, ServiceClient(breaker, identity_token=current_identity_token)
    )

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        evaluation_pb2_grpc.add_EvaluationServiceServicer_to_server(
            EvaluationService(engine, authorizer), server
        )

    async def relay_outbox(stop: asyncio.Event) -> None:
        await relay(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME, register, readiness_checks=(database_ready,), background=(relay_outbox,)
        )
    )


if __name__ == "__main__":
    main()
