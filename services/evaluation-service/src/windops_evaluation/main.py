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
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.evaluation.v1 import evaluation_pb2_grpc
from windops.external_intelligence.v1 import external_intelligence_pb2_grpc
from windops.weather.v1 import weather_pb2_grpc
from windops_auth import current_identity_token
from windops_events import Events, EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_evaluation import live_runner, replay_api
from windops_evaluation.authorizer import AuthorizationClient
from windops_evaluation.live_runner import LiveSettings
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


class _Lazy:
    """Makes its stub on first use, because a channel must be made on the running loop."""

    def __init__(self, make: Any) -> None:
        self._make, self._made = make, None

    def __getattr__(self, name: str) -> Any:
        if self._made is None:
            self._made = self._make()
        return getattr(self._made, name)


def _live_metrics() -> None:
    """The latest live run: whether it passed, and when, so an alert can say it failed or
    stopped running (M12.6b)."""
    meter = metrics.get_meter(SERVICE_NAME)

    def passed(_: Any) -> list[metrics.Observation]:
        found = live_runner.LATEST["passed"]
        return [] if found is None else [metrics.Observation(1 if found else 0)]

    def finished(_: Any) -> list[metrics.Observation]:
        at = live_runner.LATEST["finished_at"]
        return [] if at is None else [metrics.Observation(at.timestamp())]

    meter.create_observable_gauge(
        "windops_live_suite_passed", [passed], description="1 when the latest live suite run passed"
    )
    meter.create_observable_gauge(
        "windops_live_suite_finished_seconds",
        [finished],
        unit="s",
        description="When the latest live suite run finished",
    )


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

    # The live suite, where a live tenant is set (EVL-001, M12.6b).
    live_settings = LiveSettings.from_env()
    live_suite = None
    if live_settings is not None:
        stubs, clients = {}, {}
        for name, setting, make in (
            ("asset", "WINDOPS_ASSET_ADDRESS", asset_pb2_grpc.AssetServiceStub),
            ("weather", "WINDOPS_WEATHER_ADDRESS", weather_pb2_grpc.WeatherServiceStub),
            ("external-intelligence", "WINDOPS_EXTERNAL_INTELLIGENCE_ADDRESS",
             external_intelligence_pb2_grpc.ExternalIntelligenceServiceStub),
        ):  # fmt: skip
            stubs[name] = _Lazy(lambda s=setting, m=make: m(create_channel(os.environ[s], tls)))
            clients[name] = ServiceClient(
                CircuitBreaker(name, BreakerConfig(slow_call_seconds=live_runner.DEADLINES[name]),
                               on_state_change=breaker_state_metrics(metrics.get_meter_provider())),
                identity_token=current_identity_token,
            )  # fmt: skip
        providers = live_runner.ServiceProviders(
            stubs, clients, live_settings.tenant, os.environ["WINDOPS_IDENTITY_SECRET"]
        )
        live_suite = (live_settings, providers)
    _live_metrics()

    def register(server: grpc.aio.Server) -> None:
        evaluation_pb2_grpc.add_EvaluationServiceServicer_to_server(
            EvaluationService(engine, authorizer, live_suite=live_suite), server
        )

    async def live_schedule(stop: asyncio.Event) -> None:
        # A background task that returns stops the service, so with no suite it only waits.
        if live_suite is None:
            await stop.wait()
            return
        await live_runner.schedule(engine, live_suite[1], live_suite[0], stop)

    async def relay_outbox(stop: asyncio.Event) -> None:
        await relay(engine, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            # The outbox relay, and the live suite's schedule (M12.6b).
            background=(relay_outbox, live_schedule),
            # The recorded providers, only for callers with the cluster's certificates (M12.6).
            http_router=replay_api.router(engine),
            api_over_https=True,
            api_client_certificates=True,
        )
    )


if __name__ == "__main__":
    main()
