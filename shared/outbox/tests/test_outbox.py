"""The outbox's rules, on a real SQL engine. `make outbox-check` adds PostgreSQL and JetStream."""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.events.v1 import events_pb2
from windops_events import EventsUnavailableError, PublishedEvent
from windops_migrations import find_violations
from windops_outbox import (
    OUTBOX_TABLE,
    OutboxRelay,
    add_event,
    create_outbox_table,
    outbox_status,
    prune_published,
)
from windops_outbox.outbox import waiting_rows_query

ALARM = events_pb2.AlarmChanged(alarm_id="a-1", turbine_id="t-1")
STATE = Table("state", MetaData(), Column("id", Integer, primary_key=True))


def with_engine(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[[], None]:
    """Run an async test with a new in-memory database."""

    def wrapper() -> None:
        async def run() -> None:
            engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
            async with engine.begin() as connection:
                await connection.run_sync(create_outbox_table)
                await connection.run_sync(STATE.create)
            try:
                await test(engine)
            finally:
                await engine.dispose()

        asyncio.run(run())

    # Not functools.wraps, or pytest would read the signature and look for a fixture.
    wrapper.__name__ = test.__name__
    wrapper.__doc__ = test.__doc__
    return wrapper


class FakePublisher:
    def __init__(self, *, fail_on: int | None = None) -> None:
        self.sent: list[events_pb2.EventEnvelope] = []
        self.fail_on = fail_on
        self.calls = 0

    async def publish_envelope(
        self, subject: str, envelope: events_pb2.EventEnvelope
    ) -> PublishedEvent:
        self.calls += 1
        if self.fail_on is not None and self.calls >= self.fail_on:
            raise EventsUnavailableError("down")
        self.sent.append(envelope)
        return PublishedEvent(envelope.event_id, "WINDOPS_EVENTS", self.calls, False)


async def queue(engine: AsyncEngine, count: int = 1) -> list[str]:
    ids = []
    async with engine.begin() as connection:
        for _ in range(count):
            ids.append(
                await add_event(
                    connection,
                    "alarm.changed",
                    tenant_id="tenant-a",
                    payload=ALARM,
                    producer="alarm",
                )
            )
    return ids


async def rows(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as connection:
        result = await connection.execute(select(OUTBOX_TABLE).order_by(OUTBOX_TABLE.c.sequence))
        return list(result.all())


@with_engine
async def test_state_and_event_commit_together(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(insert(STATE).values(id=1))
        await add_event(
            connection, "alarm.changed", tenant_id="tenant-a", payload=ALARM, producer="alarm"
        )
    async with engine.connect() as connection:
        assert (await connection.execute(select(STATE))).all() == [(1,)]
    assert len(await rows(engine)) == 1


@with_engine
async def test_a_rollback_removes_the_event_too(engine: AsyncEngine) -> None:
    """EVT-002: no event for a state change that did not happen."""
    with pytest.raises(RuntimeError, match="later failure"):
        async with engine.begin() as connection:
            await connection.execute(insert(STATE).values(id=1))
            await add_event(
                connection, "alarm.changed", tenant_id="tenant-a", payload=ALARM, producer="alarm"
            )
            raise RuntimeError("later failure")
    assert await rows(engine) == []
    async with engine.connect() as connection:
        assert (await connection.execute(select(STATE))).all() == []


@with_engine
async def test_an_event_without_a_transaction_is_refused(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        with pytest.raises(ValueError, match="transaction"):
            await add_event(
                connection, "alarm.changed", tenant_id="tenant-a", payload=ALARM, producer="alarm"
            )


@with_engine
async def test_a_bad_event_fails_the_state_change(engine: AsyncEngine) -> None:
    """Better now, while the caller can respond, than in the relay later."""
    with pytest.raises(ValueError, match="tenant"):
        async with engine.begin() as connection:
            await connection.execute(insert(STATE).values(id=1))
            await add_event(
                connection, "alarm.changed", tenant_id="", payload=ALARM, producer="alarm"
            )
    async with engine.connect() as connection:
        assert (await connection.execute(select(STATE))).all() == []


@with_engine
async def test_the_same_event_id_cannot_be_queued_twice(engine: AsyncEngine) -> None:
    for attempt in range(2):
        if attempt == 1:
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await add_event(
                        connection,
                        "alarm.changed",
                        tenant_id="tenant-a",
                        payload=ALARM,
                        producer="alarm",
                        event_id="fixed",
                    )
        else:
            async with engine.begin() as connection:
                await add_event(
                    connection,
                    "alarm.changed",
                    tenant_id="tenant-a",
                    payload=ALARM,
                    producer="alarm",
                    event_id="fixed",
                )
    assert len(await rows(engine)) == 1


@with_engine
async def test_the_relay_sends_events_in_order_and_marks_them(engine: AsyncEngine) -> None:
    ids = await queue(engine, 3)
    publisher = FakePublisher()
    result = await OutboxRelay(engine, publisher).run_once()
    assert result.published == 3
    assert not result.unavailable
    assert [item.event_id for item in publisher.sent] == ids
    assert all(row.published_at is not None for row in await rows(engine))


@with_engine
async def test_a_sent_event_is_not_sent_again(engine: AsyncEngine) -> None:
    await queue(engine)
    publisher = FakePublisher()
    relay = OutboxRelay(engine, publisher)
    await relay.run_once()
    assert (await relay.run_once()).published == 0
    assert len(publisher.sent) == 1


@with_engine
async def test_an_outage_keeps_the_event_for_later(engine: AsyncEngine) -> None:
    """SRS 7.7: state changes commit while JetStream is down, and events wait."""
    ids = await queue(engine)
    result = await OutboxRelay(engine, FakePublisher(fail_on=1)).run_once()
    assert result.unavailable
    assert (await rows(engine))[0].published_at is None
    publisher = FakePublisher()
    await OutboxRelay(engine, publisher).run_once()
    assert [item.event_id for item in publisher.sent] == ids


@with_engine
async def test_an_outage_mid_batch_keeps_what_was_sent_and_the_order(engine: AsyncEngine) -> None:
    ids = await queue(engine, 3)
    first = FakePublisher(fail_on=2)
    result = await OutboxRelay(engine, first).run_once()
    assert (result.published, result.unavailable) == (1, True)
    stored = await rows(engine)
    assert [row.published_at is not None for row in stored] == [True, False, False]
    second = FakePublisher()
    await OutboxRelay(engine, second).run_once()
    assert [item.event_id for item in second.sent] == ids[1:]


@with_engine
async def test_the_batch_size_is_respected(engine: AsyncEngine) -> None:
    await queue(engine, 3)
    result = await OutboxRelay(engine, FakePublisher(), batch=2).run_once()
    assert result.published == 2


@with_engine
async def test_a_row_that_cannot_be_read_does_not_block_the_rest(engine: AsyncEngine) -> None:
    ids = await queue(engine, 2)
    async with engine.begin() as connection:
        await connection.execute(
            update(OUTBOX_TABLE)
            .where(OUTBOX_TABLE.c.event_id == ids[0])
            .values(envelope=b"\xff\xff not protobuf")
        )
    publisher = FakePublisher()
    result = await OutboxRelay(engine, publisher).run_once()
    assert (result.published, result.failed) == (1, 1)
    assert [item.event_id for item in publisher.sent] == [ids[1]]
    stored = await rows(engine)
    assert stored[0].failed_at is not None
    assert stored[0].failure_type == "DecodeError"
    async with engine.connect() as connection:
        status = await outbox_status(connection)
    assert (status.waiting, status.failed) == (0, 1)


@with_engine
async def test_status_reports_the_backlog(engine: AsyncEngine) -> None:
    await queue(engine, 2)
    async with engine.connect() as connection:
        status = await outbox_status(connection)
    assert status.waiting == 2
    assert status.oldest_waiting_seconds >= 0
    await OutboxRelay(engine, FakePublisher()).run_once()
    async with engine.connect() as connection:
        status = await outbox_status(connection)
    assert (status.waiting, status.oldest_waiting_seconds) == (0, 0.0)


@with_engine
async def test_pruning_never_removes_an_unsent_event(engine: AsyncEngine) -> None:
    await queue(engine)
    async with engine.begin() as connection:
        assert await prune_published(connection, older_than=timedelta(seconds=1)) == 0
    assert len(await rows(engine)) == 1


@with_engine
async def test_pruning_keeps_recent_sent_events(engine: AsyncEngine) -> None:
    await queue(engine)
    await OutboxRelay(engine, FakePublisher()).run_once()
    async with engine.begin() as connection:
        assert await prune_published(connection, older_than=timedelta(days=7)) == 0
    assert len(await rows(engine)) == 1


@with_engine
async def test_pruning_needs_a_positive_age(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        with pytest.raises(ValueError, match="positive"):
            await prune_published(connection, older_than=timedelta(0))


@with_engine
async def test_run_forever_stops_when_told(engine: AsyncEngine) -> None:
    ids = await queue(engine)
    publisher = FakePublisher()
    stop = asyncio.Event()
    task = asyncio.create_task(OutboxRelay(engine, publisher).run_forever(stop))
    while not publisher.sent:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert [item.event_id for item in publisher.sent] == ids


def test_relays_take_different_rows_on_postgresql() -> None:
    """SQLite ignores this, so check the SQL that PostgreSQL receives."""
    query = waiting_rows_query(10)
    sql = str(query.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined,no-untyped-call]
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_the_table_migration_is_an_expand_step(tmp_path: Path) -> None:
    """Adding the outbox must pass the check every service's migrations must pass."""
    path = tmp_path / "rev_1_outbox.py"
    path.write_text(
        "from alembic import op\nfrom windops_outbox import create_outbox_table\n\n"
        'phase = "expand"\n\n\ndef upgrade() -> None:\n'
        "    create_outbox_table(op.get_bind())\n"
    )
    assert find_violations(path) == []
