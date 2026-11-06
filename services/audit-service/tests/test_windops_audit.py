"""The audit service's read API, through a real gRPC server with signed identities."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.events.v1 import events_pb2
from windops_audit.main import record_event, registrar
from windops_audit.records import AUDIT_RECORDS, SERVICE_NAME
from windops_events import Event, RetryLaterError
from windops_grpc import (
    ServiceRuntime,
    Settings,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_outbox import create_outbox_table

SECRET = "x" * 32
SETTINGS = Settings(
    environment="test",
    version="test",
    identity_secret=SECRET,
    grpc_port=0,
    http_port=0,
    shutdown_grace_seconds=1,
)


def event(event_id: str, tenant: str = "tenant-a") -> Event:
    return Event(
        subject="audit.requested.alarm",
        event_id=event_id,
        tenant_id=tenant,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        producer="alarm",
        payload=events_pb2.AuditRequested(
            action="alarm.acknowledged", actor_type="user", actor_id="u-1", outcome="succeeded"
        ),
        trace_parent="",
        request_id="r-1",
    )


READER = "reader"


def metadata(tenant: str = "tenant-a", user: str = READER) -> tuple[tuple[str, str], ...]:
    token = sign_identity(VerifiedIdentity(subject=user, tenant_id=tenant, user_id=user), SECRET)
    return (("x-windops-identity", token),)


class FakeAuthorizer:
    """Allows audit.read to READER only. The Authorization Service has its own tests."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.asked: list[tuple[str, str]] = []

    async def allowed(self, permission: str) -> bool:
        user = current_identity().user_id or ""
        self.asked.append((user, permission))
        if self.down:
            raise UnavailableError("Permissions cannot be checked right now")
        return user == READER and permission == "audit.read"


def with_service(
    scenario: Callable[[Any, grpc.aio.Channel], Awaitable[None]],
    *events: Event,
    authorizer: FakeAuthorizer | None = None,
) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(AUDIT_RECORDS.metadata.create_all)
            await connection.run_sync(create_outbox_table)
        for item in events:
            await record_event(engine, item)
        runtime = ServiceRuntime(
            SERVICE_NAME,
            SETTINGS,
            registrar(engine, authorizer or FakeAuthorizer()),
            install_global_telemetry=False,
        )
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                await scenario(audit_pb2_grpc.AuditServiceStub(channel), channel)
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_service_starts_healthy_and_stops() -> None:
    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        health = health_pb2_grpc.HealthStub(channel)
        response = await health.Check(health_pb2.HealthCheckRequest(), timeout=5)
        assert response.status == health_pb2.HealthCheckResponse.SERVING

    with_service(scenario)


def test_a_caller_sees_only_its_own_tenants_records() -> None:
    """GRP-004: the tenant comes from the verified identity."""

    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        request = audit_pb2.ListAuditRecordsRequest()
        mine = await stub.ListAuditRecords(request, metadata=metadata("tenant-a"), timeout=5)
        theirs = await stub.ListAuditRecords(request, metadata=metadata("tenant-b"), timeout=5)
        assert [item.event_id for item in mine.records] == ["e-1"]
        assert [item.event_id for item in theirs.records] == ["e-2"]

    with_service(scenario, event("e-1", "tenant-a"), event("e-2", "tenant-b"))


def test_a_call_with_no_identity_is_refused() -> None:
    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.ListAuditRecords(audit_pb2.ListAuditRecordsRequest(), timeout=5)
        assert error.value.code() is grpc.StatusCode.UNAUTHENTICATED

    with_service(scenario, event("e-1"))


def test_reading_needs_audit_read() -> None:
    """SRS 3.2: only Platform and Tenant Administrators and Auditors read the trail."""
    authorizer = FakeAuthorizer()

    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        for call, request in (
            (stub.ListAuditRecords, audit_pb2.ListAuditRecordsRequest()),
            (stub.VerifyAuditChain, audit_pb2.VerifyAuditChainRequest()),
        ):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await call(request, metadata=metadata(user="operator"), timeout=5)
            assert error.value.code() is grpc.StatusCode.PERMISSION_DENIED
        assert authorizer.asked == [("operator", "audit.read")] * 2

    with_service(scenario, event("e-1"), authorizer=authorizer)


def test_when_permissions_cannot_be_checked_nothing_is_read() -> None:
    """SRS 7.7: a failure to check denies."""

    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.ListAuditRecords(
                audit_pb2.ListAuditRecordsRequest(), metadata=metadata(), timeout=5
            )
        assert error.value.code() is grpc.StatusCode.UNAVAILABLE

    with_service(scenario, event("e-1"), authorizer=FakeAuthorizer(down=True))


def test_records_can_be_listed_by_action() -> None:
    """For the administrator console's access denials (SRS 9.4)."""
    denied = event("e-2")
    denied.payload.CopyFrom(
        events_pb2.AuditRequested(
            action="authorization.denied", actor_type="user", actor_id="u-1", outcome="denied"
        )
    )

    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        only = await stub.ListAuditRecords(
            audit_pb2.ListAuditRecordsRequest(action="authorization.denied"),
            metadata=metadata(),
            timeout=5,
        )
        assert [item.event_id for item in only.records] == ["e-2"]

    with_service(scenario, event("e-1"), denied, event("e-3"))


def test_records_are_returned_a_page_at_a_time() -> None:
    """GRP-008."""

    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        first = await stub.ListAuditRecords(
            audit_pb2.ListAuditRecordsRequest(page_size=2), metadata=metadata(), timeout=5
        )
        second = await stub.ListAuditRecords(
            audit_pb2.ListAuditRecordsRequest(page_size=2, page_token=first.next_page_token),
            metadata=metadata(),
            timeout=5,
        )
        assert [item.event_id for item in first.records] == ["e-1", "e-2"]
        assert [item.event_id for item in second.records] == ["e-3"]
        assert second.next_page_token == ""

    with_service(scenario, event("e-1"), event("e-2"), event("e-3"))


@pytest.mark.parametrize(
    "request_message",
    [
        audit_pb2.ListAuditRecordsRequest(page_token="not-a-number"),
        audit_pb2.ListAuditRecordsRequest(page_size=-1),
        audit_pb2.ListAuditRecordsRequest(action="x" * 129),
    ],
    ids=["bad_token", "negative_size", "long_action"],
)
def test_an_invalid_page_request_is_refused(
    request_message: audit_pb2.ListAuditRecordsRequest,
) -> None:
    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.ListAuditRecords(request_message, metadata=metadata(), timeout=5)
        assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_the_chain_can_be_verified_through_the_api() -> None:
    async def scenario(stub: Any, channel: grpc.aio.Channel) -> None:
        result = await stub.VerifyAuditChain(
            audit_pb2.VerifyAuditChainRequest(), metadata=metadata(), timeout=5
        )
        assert (result.valid, result.records_checked) == (True, 2)

    with_service(scenario, event("e-1"), event("e-2"))


class FailingBegin:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.attempts = 0

    def begin(self) -> "FailingBegin":
        self.attempts += 1
        return self

    async def __aenter__(self) -> None:
        raise self.error

    async def __aexit__(self, *details: object) -> None:
        return None


def test_a_database_outage_makes_the_event_wait() -> None:
    """SRS 7.7: however long the outage, the event is not dead-lettered."""
    failing = FailingBegin(OperationalError("SELECT 1", {}, Exception("down")))
    with pytest.raises(RetryLaterError):
        asyncio.run(record_event(_engine(failing), event("e-1")))
    assert failing.attempts == 1


def test_losing_a_race_to_another_replica_tries_again() -> None:
    failing = FailingBegin(IntegrityError("INSERT", {}, Exception("taken")))
    with pytest.raises(RetryLaterError, match="busy"):
        asyncio.run(record_event(_engine(failing), event("e-1")))
    assert failing.attempts == 5


def _engine(fake: FailingBegin) -> AsyncEngine:
    return fake  # type: ignore[return-value]


def test_stored_records_are_counted_by_action_once_each() -> None:
    """For the Security dashboard: a repeat is not counted, as it is not stored again."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
    from windops_audit.main import records_counter

    reader = InMemoryMetricReader()
    counter = records_counter(MeterProvider(metric_readers=[reader]))
    denied = event("e-2")
    denied.payload.CopyFrom(
        events_pb2.AuditRequested(
            action="authorization.denied", actor_type="user", actor_id="u-1", outcome="denied"
        )
    )

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(AUDIT_RECORDS.metadata.create_all)
            await connection.run_sync(create_outbox_table)
        for item in (event("e-1"), denied, denied):
            await record_event(engine, item, counter)
        await engine.dispose()

    asyncio.run(run())
    data = reader.get_metrics_data()
    assert data is not None
    points = [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "windops.audit.records"
        for point in metric.data.data_points
        if isinstance(point, NumberDataPoint)
    ]
    counted = {
        ((p.attributes or {})["action"], (p.attributes or {})["outcome"]): p.value for p in points
    }
    assert {key: value for key, value in counted.items() if value} == {
        ("alarm.acknowledged", "succeeded"): 1,
        ("authorization.denied", "denied"): 1,
    }
    # What the alerts watch is there from the start, at 0, so the first one is seen as a change.
    assert counted[("authorization.break_glass.started", "succeeded")] == 0
