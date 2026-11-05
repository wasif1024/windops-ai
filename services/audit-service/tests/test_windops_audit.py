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
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, sign_identity
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


def metadata(tenant: str = "tenant-a") -> tuple[tuple[str, str], ...]:
    token = sign_identity(VerifiedIdentity(subject="check", tenant_id=tenant), SECRET)
    return (("x-windops-identity", token),)


def with_service(
    scenario: Callable[[Any, grpc.aio.Channel], Awaitable[None]],
    *events: Event,
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
            SERVICE_NAME, SETTINGS, registrar(engine), install_global_telemetry=False
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
    ],
    ids=["bad_token", "negative_size"],
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
