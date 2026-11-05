"""The audit chain's rules, on a real SQL engine. `make audit-check` adds PostgreSQL."""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import pytest
from google.protobuf.message import Message
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.events.v1 import events_pb2
from windops_audit.records import (
    AUDIT_RECORDS,
    FIRST_PREVIOUS_HASH,
    InvalidAuditRequestError,
    append_record,
    compute_hash,
    list_records,
    verify_chain,
)
from windops_events import Event
from windops_outbox import OUTBOX_TABLE, create_outbox_table


def request(**overrides: str) -> events_pb2.AuditRequested:
    values = {
        "action": "alarm.acknowledged",
        "actor_type": "user",
        "actor_id": "u-1",
        "outcome": "succeeded",
    }
    return events_pb2.AuditRequested(**{**values, **overrides})


def event(event_id: str = "e-1", tenant: str = "tenant-a", payload: Message | None = None) -> Event:
    return Event(
        subject="audit.requested.alarm",
        event_id=event_id,
        tenant_id=tenant,
        occurred_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
        producer="alarm",
        payload=payload or request(),
        trace_parent="",
        request_id="r-1",
    )


def with_engine(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[[], None]:
    def wrapper() -> None:
        async def run() -> None:
            engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
            async with engine.begin() as connection:
                await connection.run_sync(AUDIT_RECORDS.metadata.create_all)
                await connection.run_sync(create_outbox_table)
            try:
                await test(engine)
            finally:
                await engine.dispose()

        asyncio.run(run())

    # Not functools.wraps, or pytest would read the signature and look for a fixture.
    wrapper.__name__ = test.__name__
    wrapper.__doc__ = test.__doc__
    return wrapper


async def append(engine: AsyncEngine, *events: Event) -> list[bool]:
    results = []
    for item in events:
        async with engine.begin() as connection:
            results.append(await append_record(connection, item))
    return results


async def stored(engine: AsyncEngine, tenant: str = "tenant-a") -> list[Any]:
    async with engine.connect() as connection:
        return await list_records(connection, tenant, limit=100, after_sequence=0)


async def verify(engine: AsyncEngine, tenant: str = "tenant-a") -> Any:
    async with engine.connect() as connection:
        return await verify_chain(connection, tenant)


@with_engine
async def test_a_record_holds_what_the_request_says(engine: AsyncEngine) -> None:
    payload = request(on_behalf_of="u-9", resource_type="alarm", resource_id="a-1")
    await append(engine, event(payload=payload))
    row = (await stored(engine))[0]
    assert (row.action, row.actor_id, row.on_behalf_of) == ("alarm.acknowledged", "u-1", "u-9")
    assert (row.resource_type, row.resource_id, row.outcome) == ("alarm", "a-1", "succeeded")
    assert row.producer == "alarm"


@with_engine
async def test_a_repeated_event_creates_one_record(engine: AsyncEngine) -> None:
    """The Phase 2 exit gate."""
    assert await append(engine, event(), event()) == [True, False]
    assert len(await stored(engine)) == 1


@with_engine
async def test_a_repeated_event_announces_once(engine: AsyncEngine) -> None:
    await append(engine, event(), event())
    async with engine.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE))).all()
    assert [row.subject for row in rows] == ["audit.recorded"]


@with_engine
async def test_a_failed_record_is_not_announced(engine: AsyncEngine) -> None:
    """The record and its announcement commit together (EVT-002)."""
    with pytest.raises(RuntimeError, match="later"):
        async with engine.begin() as connection:
            await append_record(connection, event())
            raise RuntimeError("later failure")
    assert await stored(engine) == []
    async with engine.connect() as connection:
        assert (await connection.execute(select(OUTBOX_TABLE))).all() == []


@with_engine
async def test_records_link_to_the_one_before(engine: AsyncEngine) -> None:
    await append(engine, event("e-1"), event("e-2"))
    first, second = await stored(engine)
    assert first.previous_hash == FIRST_PREVIOUS_HASH
    assert second.previous_hash == first.record_hash


@with_engine
async def test_the_database_refuses_a_fork_in_the_chain(engine: AsyncEngine) -> None:
    """Even a bug cannot give one record two successors (FR-AUD-002)."""
    await append(engine, event("e-1"))
    row = (await stored(engine))[0]
    fields = {key: value for key, value in row._mapping.items() if key != "sequence"}
    fork = {**fields, "event_id": "e-fork", "record_hash": "another"}
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(AUDIT_RECORDS.insert().values(**fork))


@with_engine
async def test_each_tenant_has_its_own_chain(engine: AsyncEngine) -> None:
    await append(engine, event("e-1", "tenant-a"), event("e-2", "tenant-b"))
    assert (await stored(engine, "tenant-b"))[0].previous_hash == FIRST_PREVIOUS_HASH


@with_engine
async def test_a_tenant_reads_only_its_own_records(engine: AsyncEngine) -> None:
    await append(engine, event("e-1", "tenant-a"), event("e-2", "tenant-b"))
    assert [row.event_id for row in await stored(engine, "tenant-a")] == ["e-1"]


@with_engine
async def test_pages_continue_after_the_last_record(engine: AsyncEngine) -> None:
    await append(engine, event("e-1"), event("e-2"), event("e-3"))
    async with engine.connect() as connection:
        first = await list_records(connection, "tenant-a", limit=2, after_sequence=0)
        rest = await list_records(
            connection, "tenant-a", limit=2, after_sequence=first[-1].sequence
        )
    assert [row.event_id for row in [*first, *rest]] == ["e-1", "e-2", "e-3"]


@pytest.mark.parametrize("missing", ["action", "actor_id", "outcome"])
def test_a_request_missing_a_required_field_is_refused(missing: str) -> None:
    @with_engine
    async def scenario(engine: AsyncEngine) -> None:
        with pytest.raises(InvalidAuditRequestError, match="needs"):
            await append(engine, event(payload=request(**{missing: ""})))

    scenario()


@with_engine
async def test_the_wrong_message_is_refused(engine: AsyncEngine) -> None:
    with pytest.raises(InvalidAuditRequestError, match="not an audit request"):
        await append(engine, event(payload=events_pb2.CaseUpdated(case_id="c-1")))


@with_engine
async def test_an_untouched_chain_is_valid(engine: AsyncEngine) -> None:
    await append(engine, event("e-1"), event("e-2"), event("e-3"))
    result = await verify(engine)
    assert (result.valid, result.records_checked) == (True, 3)


@with_engine
async def test_an_empty_chain_is_valid(engine: AsyncEngine) -> None:
    assert (await verify(engine)).valid


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE audit_records SET outcome = 'denied' WHERE event_id = 'e-2'",
        "UPDATE audit_records SET actor_id = 'someone-else' WHERE event_id = 'e-2'",
        "UPDATE audit_records SET record_hash = 'x' WHERE event_id = 'e-2'",
    ],
)
def test_a_changed_record_is_detected(change: str) -> None:
    """FR-AUD-002."""

    @with_engine
    async def scenario(engine: AsyncEngine) -> None:
        await append(engine, event("e-1"), event("e-2"), event("e-3"))
        async with engine.begin() as connection:
            await connection.execute(text(change))
        result = await verify(engine)
        assert (result.valid, result.records_checked, result.first_invalid_sequence) == (
            False,
            1,
            2,
        )

    scenario()


@with_engine
async def test_a_removed_record_is_detected(engine: AsyncEngine) -> None:
    await append(engine, event("e-1"), event("e-2"), event("e-3"))
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM audit_records WHERE event_id = 'e-2'"))
    result = await verify(engine)
    assert (result.valid, result.first_invalid_sequence) == (False, 3)


def test_the_same_content_always_gives_the_same_hash() -> None:
    fields = {"a": "1", "at": datetime(2026, 1, 1, tzinfo=UTC)}
    assert compute_hash(fields) == compute_hash(dict(reversed(fields.items())))


def test_a_time_hashes_the_same_in_any_form() -> None:
    assert compute_hash({"at": datetime(2026, 1, 1, tzinfo=UTC)}) == compute_hash(
        {"at": datetime(2026, 1, 1)}
    )


def test_different_content_gives_a_different_hash() -> None:
    assert compute_hash({"a": "1"}) != compute_hash({"a": "2"})
