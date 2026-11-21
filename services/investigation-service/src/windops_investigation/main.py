"""Start the Investigation Service: its API, its fault.detected consumer and its relay."""

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import grpc
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.agent_runtime.v1 import agent_runtime_pb2_grpc
from windops.alarm.v1 import alarm_pb2_grpc
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.identity.v1 import identity_pb2_grpc
from windops.inventory.v1 import inventory_pb2, inventory_pb2_grpc
from windops.investigation.v1 import investigation_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2_grpc
from windops_auth import current_identity_token
from windops_events import Event, Events, EventsUnavailableError, keep_consuming, open_events
from windops_grpc import ServiceClient, UnavailableError, create_channel, run_service
from windops_migrations import create_service_engine
from windops_observability import breaker_state_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, BreakerOpenError, CircuitBreaker

from windops_investigation import stale
from windops_investigation.consumer import SUBJECT, handle
from windops_investigation.evidence import Builder, keep_building
from windops_investigation.investigations import (
    MODES,
    DelegationClient,
    Keeper,
    RuntimeClient,
    keep_investigating,
)
from windops_investigation.service import (
    AssetClient,
    AuthorizationClient,
    EvidenceSources,
    InvestigationService,
)
from windops_investigation.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5


Handler = Callable[[Event], Awaitable[Any]]


async def consume_and_relay(
    engine: AsyncEngine, stop: asyncio.Event, consumers: list[tuple[str, str, Handler]]
) -> None:
    """Read each subject on its own, and relay the outbox. Keep trying to connect, because an
    outage must not restart this service (RES-006)."""
    events: Events | None = None
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

    def handled(work: Handler) -> Callable[[Event], Awaitable[None]]:
        async def handler(event: Event) -> None:
            await work(event)

        return handler

    tasks = [asyncio.create_task(OutboxRelay(engine, events.publisher).run_forever(stop))]
    for subject, durable, work in consumers:
        consumer = events.consumer(subject=subject, durable=durable, handler=handled(work))
        tasks.append(asyncio.create_task(keep_consuming(consumer, stop, subject)))
    try:
        await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await events.close()


class StaleOwners:
    """The turbine and a proposal's parts, for the stale rules, asked as this service."""

    def __init__(self, assets: Any, inventory: Any, client: ServiceClient) -> None:
        self._assets, self._inventory, self._client = assets, inventory, client

    async def turbine(self, turbine_id: str) -> Any:
        return await self._assets.turbine(turbine_id)

    async def parts(self, turbine: Any, lines: list[tuple[str, int]]) -> list[Any]:
        request = inventory_pb2.CheckPartsRequest(
            model=inventory_pb2.ModelRef(manufacturer=turbine.model.manufacturer,
                                         model=turbine.model.model),
            wind_farm_id=turbine.wind_farm_id,
            lines=[inventory_pb2.PartLine(part_number=n, quantity=q) for n, q in lines],
        )  # fmt: skip
        try:
            answer = await self._client.call(self._inventory().CheckParts, request,
                                             idempotent=True, deadline_seconds=3.0)  # fmt: skip
        except grpc.aio.AioRpcError as error:
            raise UnavailableError("The Inventory Service cannot be asked right now") from error
        except (BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("The Inventory Service cannot be asked right now") from error
        return list(answer.checks)


class _Lazy:
    """Makes its client on first use, because a channel must be made on the running loop."""

    def __init__(self, make: Any) -> None:
        self._make, self._made = make, None

    def __getattr__(self, name: str) -> Any:
        if self._made is None:
            self._made = self._make()
        return getattr(self._made, name)


def _client(dependency: str, slow_call_seconds: float = 1.0) -> ServiceClient:
    breaker = CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=slow_call_seconds),
        on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
    )
    # The caller's own identity goes along, so the other service decides for them.
    return ServiceClient(breaker, identity_token=current_identity_token)


def main() -> None:
    engine = create_service_engine(pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    authorizer = _Lazy(
        lambda: AuthorizationClient(
            authorization_pb2_grpc.AuthorizationServiceStub(
                create_channel(os.environ["WINDOPS_AUTHORIZATION_ADDRESS"], tls)
            ),
            _client("authorization"),
        )
    )
    assets = _Lazy(
        lambda: AssetClient(
            asset_pb2_grpc.AssetServiceStub(
                create_channel(os.environ["WINDOPS_ASSET_ADDRESS"], tls)
            ),
            _client("asset"),
        )
    )

    def stub(make: Any, setting: str) -> Any:
        return make(create_channel(os.environ[setting], tls))

    sources = _Lazy(
        lambda: EvidenceSources(
            assets,
            {
                "telemetry": stub(telemetry_pb2_grpc.TelemetryServiceStub,
                                  "WINDOPS_TELEMETRY_ADDRESS"),
                "alarm": stub(alarm_pb2_grpc.AlarmServiceStub, "WINDOPS_ALARM_ADDRESS"),
                "fault-detection": stub(fault_detection_pb2_grpc.FaultRuleServiceStub,
                                        "WINDOPS_FAULT_DETECTION_ADDRESS"),
            },
            {
                # Freezing a day of telemetry is a long call, not a slow one.
                "telemetry": _client("telemetry", slow_call_seconds=45.0),
                "alarm": _client("alarm"),
                "fault-detection": _client("fault-detection"),
            },
        )
    )  # fmt: skip
    secret = os.environ["WINDOPS_IDENTITY_SECRET"]
    builder = Builder(engine, sources, secret)
    # How a case's agents run (ADR 0025). Anything unknown is refused at start.
    agents = os.environ.get("WINDOPS_INVESTIGATION_AGENTS", "rehearse")
    if agents not in MODES:
        raise SystemExit(f"WINDOPS_INVESTIGATION_AGENTS must be one of: {', '.join(MODES)}")
    channels: dict[str, Any] = {}

    def channel(setting: str) -> Any:
        # Made on first use, on the running loop.
        if setting not in channels:
            channels[setting] = create_channel(os.environ[setting], tls)
        return channels[setting]

    runtime = RuntimeClient(
        lambda: agent_runtime_pb2_grpc.AgentRuntimeServiceStub(
            channel("WINDOPS_AGENT_RUNTIME_ADDRESS")
        ),
        _client("agent-runtime"),
    )
    delegations = DelegationClient(
        lambda: identity_pb2_grpc.IdentityServiceStub(channel("WINDOPS_IDENTITY_ADDRESS")),
        _client("identity"),
    )
    # A run's cost cap below the policy's, when the environment sets one (ADR 0027).
    cost_cap = os.environ.get("WINDOPS_INVESTIGATION_COST_BUDGET_USD", "")
    keeper = Keeper(engine, runtime, delegations, secret, agents, cost_budget_usd=cost_cap)

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        investigation_pb2_grpc.add_InvestigationServiceServicer_to_server(
            InvestigationService(
                engine, authorizer, assets, sources=sources, delegations=delegations, agents=agents
            ),
            server,
        )

    owners = StaleOwners(
        assets,
        lambda: inventory_pb2_grpc.InventoryServiceStub(channel("WINDOPS_INVENTORY_ADDRESS")),
        _client("inventory"),
    )
    investigate_cases = agents != "off"

    async def detected(event: Event) -> None:
        await handle(engine, event, investigate_cases)

    async def evidence_changed(event: Event) -> None:
        await stale.on_evidence(engine, event, owners, secret)

    async def consume(stop: asyncio.Event) -> None:
        await consume_and_relay(engine, stop, [
            (SUBJECT, SERVICE_NAME, detected),
            # The stale rules (SRS 8.5, M10.3), each subject on its own durable consumer.
            (stale.ALARM_SUBJECT, f"{SERVICE_NAME}-alarms", lambda e: stale.on_alarm(engine, e)),
            (stale.EVIDENCE_SUBJECT, f"{SERVICE_NAME}-evidence", evidence_changed),
            (stale.SUBMITTED_SUBJECT, f"{SERVICE_NAME}-proposals",
             lambda e: stale.on_submitted(engine, e)),
        ])  # fmt: skip

    async def sweep(stop: asyncio.Event) -> None:
        await stale.keep_sweeping(engine, stop)

    async def build(stop: asyncio.Event) -> None:
        await keep_building(builder, stop)

    async def investigate(stop: asyncio.Event) -> None:
        await keep_investigating(keeper, stop)

    asyncio.run(
        run_service(
            SERVICE_NAME,
            register,
            readiness_checks=(database_ready,),
            background=(consume, build, investigate, sweep),
        )
    )


if __name__ == "__main__":
    main()
