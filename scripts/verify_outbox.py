#!/usr/bin/env python3
"""Prove the outbox works against the real PostgreSQL and JetStream on the local cluster.

Run `make secrets` and `make infra` first. This uses a throwaway database and login, and stops NATS
for a moment. Its database, events and consumers are removed afterwards.
"""

import asyncio
import contextlib
import secrets
import subprocess
import sys
import uuid

from gateway_routes import port_forward
from nats.aio.client import Client
from nats.errors import Error as NatsError
from nats_access import ADMIN_USER as NATS_ADMIN
from nats_access import SECRETS_FILE as NATS_SECRETS
from provision_postgres import (
    ADMIN_KEY,
    ADMIN_USER,
    CONTEXT,
    NAMESPACE,
    POD,
    database_name,
    provisioning_sql,
    read_passwords,
)
from sqlalchemy import URL, Column, Integer, MetaData, Table, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from windops.events.v1 import events_pb2
from windops_events import (
    EventPublisher,
    EventsUnavailableError,
    PublishedEvent,
    connect,
    events_breaker,
)
from windops_outbox import (
    OUTBOX_TABLE,
    OutboxRelay,
    add_event,
    create_outbox_table,
    outbox_status,
)

SHORT = "outbox-probe"
DATABASE = database_name(SHORT)
NATS_STATEFULSET = "statefulset/windops-nats"
NATS_POD = "windops-nats-0"
EVENTS_STREAM = "WINDOPS_EVENTS"
TENANT = "outbox-check"
RUN = uuid.uuid4().hex[:8]
ALARM = events_pb2.AlarmChanged(alarm_id=f"outbox-{RUN}", turbine_id="check")
STATE = Table("probe_state", MetaData(), Column("id", Integer, primary_key=True))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, stdin: str | None = None, required: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={NAMESPACE}", *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if required and result.returncode != 0:
        # Not the command itself, because stdin can hold a password.
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def psql(sql: str, *, required: bool = True) -> None:
    kubectl(
        "exec", "--stdin", POD, "--", "psql", "--no-psqlrc", "--quiet", "--set=ON_ERROR_STOP=1",
        f"--username={ADMIN_USER}", "--dbname=postgres",
        stdin=sql, required=required,
    )  # fmt: skip


def create_database(password: str) -> None:
    psql(provisioning_sql({SHORT: password}))


def remove_database() -> None:
    psql(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE);", required=False)
    psql(f"DROP ROLE IF EXISTS {DATABASE};", required=False)


def engine_for(port: int, password: str) -> AsyncEngine:
    return create_async_engine(
        URL.create(
            "postgresql+psycopg",
            host="127.0.0.1",
            port=port,
            database=DATABASE,
            username=DATABASE,
            password=password,
        )
    )


async def nats_login(port: int, passwords: dict[str, str], service: str = "alarm") -> Client:
    return await connect(
        url=f"nats://127.0.0.1:{port}",
        username=database_name(service),
        password=passwords[service],
        name="outbox-check",
    )


async def stored_event_ids(client: Client) -> list[str]:
    """Read this run's events from the stream, oldest first, without a lasting consumer."""
    js = client.jetstream()
    reader = await js.pull_subscribe("alarm.changed", stream=EVENTS_STREAM)
    found: list[str] = []
    try:
        while True:
            try:
                messages = await reader.fetch(100, timeout=1)
            except TimeoutError:
                return found
            for message in messages:
                envelope = events_pb2.EventEnvelope.FromString(message.data)
                if envelope.alarm_changed.alarm_id == ALARM.alarm_id:
                    found.append(envelope.event_id)
                await message.ack()
    finally:
        await reader.unsubscribe()


async def queue(engine: AsyncEngine, state_id: int) -> str:
    async with engine.begin() as connection:
        await connection.execute(insert(STATE).values(id=state_id))
        return await add_event(
            connection, "alarm.changed", tenant_id=TENANT, payload=ALARM, producer="alarm"
        )


async def state_ids(engine: AsyncEngine) -> list[int]:
    async with engine.connect() as connection:
        return [row[0] for row in (await connection.execute(select(STATE))).all()]


async def check_table(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(create_outbox_table)
        await connection.run_sync(STATE.create)
    check(True, "a service's own login can create the outbox table")


async def check_commit_and_rollback(engine: AsyncEngine, relay: OutboxRelay) -> str:
    committed = await queue(engine, 1)
    try:
        async with engine.begin() as connection:
            await connection.execute(insert(STATE).values(id=2))
            await add_event(
                connection, "alarm.changed", tenant_id=TENANT, payload=ALARM, producer="alarm"
            )
            raise LookupError("a later step fails")
    except LookupError:
        pass
    check(await state_ids(engine) == [1], "a rollback removes the state change")
    async with engine.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE.c.event_id))).scalars().all()
    check(list(rows) == [committed], "a rollback removes its event too (EVT-002)")
    result = await relay.run_once()
    check(result.published == 1, "the relay publishes the committed event")
    return committed


class CrashesAfterPublishing:
    """Stands in for a relay process dying after JetStream stored the event."""

    def __init__(self, publisher: EventPublisher) -> None:
        self._publisher = publisher
        self.duplicate = False

    async def publish_envelope(
        self, subject: str, envelope: events_pb2.EventEnvelope
    ) -> PublishedEvent:
        result = await self._publisher.publish_envelope(subject, envelope)
        self.duplicate = result.duplicate
        raise KeyboardInterrupt


class RecordingPublisher:
    def __init__(self, publisher: EventPublisher) -> None:
        self._publisher = publisher
        self.results: list[PublishedEvent] = []

    async def publish_envelope(
        self, subject: str, envelope: events_pb2.EventEnvelope
    ) -> PublishedEvent:
        result = await self._publisher.publish_envelope(subject, envelope)
        self.results.append(result)
        return result


async def check_crash(engine: AsyncEngine, publisher: EventPublisher) -> str:
    event_id = await queue(engine, 3)
    crashing = CrashesAfterPublishing(publisher)
    with contextlib.suppress(KeyboardInterrupt):
        await OutboxRelay(engine, crashing).run_once()
    async with engine.connect() as connection:
        status = await outbox_status(connection)
    check(status.waiting == 1, "a crash after publishing leaves the row unsent, so nothing is lost")
    recording = RecordingPublisher(publisher)
    await OutboxRelay(engine, recording).run_once()
    check(
        len(recording.results) == 1 and recording.results[0].duplicate,
        "sending it again is recognized as a duplicate, and not stored twice",
    )
    return event_id


async def check_two_relays(engine: AsyncEngine, publisher: EventPublisher) -> list[str]:
    ids = [await queue(engine, 100 + index) for index in range(20)]
    recording = RecordingPublisher(publisher)
    results = await asyncio.gather(
        OutboxRelay(engine, recording, batch=5).run_once(),
        OutboxRelay(engine, recording, batch=5).run_once(),
    )
    while (await OutboxRelay(engine, recording, batch=5).run_once()).published:
        pass
    check(sum(item.published for item in results) > 0, "two relays run at the same time")
    check(
        len(recording.results) == 20 and not any(item.duplicate for item in recording.results),
        "two relays never take the same row",
    )
    return ids


async def check_outage(engine: AsyncEngine, passwords: dict[str, str]) -> str:
    """Take JetStream away for real, and show state changes still commit."""
    with port_forward(f"pod/{NATS_POD}", NAMESPACE, 4222) as port:
        client = await nats_login(port, passwords)
        publisher = EventPublisher(client.jetstream(), producer="alarm", breaker=events_breaker())
        relay = OutboxRelay(engine, publisher)
        kubectl("scale", NATS_STATEFULSET, "--replicas=0")
        kubectl("wait", f"pod/{NATS_POD}", "--for=delete", "--timeout=120s")
        event_id = await queue(engine, 200)
        check(200 in await state_ids(engine), "a state change commits while JetStream is down")
        result = await relay.run_once()
        check(result.unavailable and result.published == 0, "the relay holds the event")
        async with engine.connect() as connection:
            status = await outbox_status(connection)
        check(status.waiting == 1, "the waiting event shows in the backlog")
        await client.close()
    kubectl("scale", NATS_STATEFULSET, "--replicas=1")
    kubectl("rollout", "status", NATS_STATEFULSET, "--timeout=180s")
    kubectl("wait", f"pod/{NATS_POD}", "--for=condition=Ready", "--timeout=180s")
    return event_id


async def deliver_after_outage(engine: AsyncEngine, publisher: EventPublisher) -> None:
    result = await OutboxRelay(engine, publisher).run_once()
    check(result.published == 1, "the waiting event is published when JetStream returns")
    async with engine.connect() as connection:
        status = await outbox_status(connection)
    check(status.waiting == 0, "the backlog is empty again")


async def clean_up_events(port: int, passwords: dict[str, str]) -> None:
    client = await connect(
        url=f"nats://127.0.0.1:{port}",
        username=NATS_ADMIN,
        password=passwords[ADMIN_KEY],
        name="outbox-check-clean",
    )
    try:
        js = client.jetstream()
        reader = await js.pull_subscribe("alarm.changed", stream=EVENTS_STREAM)
        while True:
            try:
                messages = await reader.fetch(100, timeout=1)
            except TimeoutError:
                break
            for message in messages:
                if ALARM.alarm_id.encode() in message.data and message.metadata.sequence:
                    await js.delete_msg(EVENTS_STREAM, message.metadata.sequence.stream)
                await message.ack()
        await reader.unsubscribe()
    finally:
        await client.close()


async def run(db_port: int, password: str, passwords: dict[str, str]) -> None:
    engine = engine_for(db_port, password)
    try:
        await check_table(engine)
        with port_forward(f"pod/{NATS_POD}", NAMESPACE, 4222) as port:
            client = await nats_login(port, passwords)
            publisher = EventPublisher(
                client.jetstream(), producer="alarm", breaker=events_breaker()
            )
            first = await check_commit_and_rollback(engine, OutboxRelay(engine, publisher))
            crashed = await check_crash(engine, publisher)
            many = await check_two_relays(engine, publisher)
            await client.close()
        late = await check_outage(engine, passwords)
        with port_forward(f"pod/{NATS_POD}", NAMESPACE, 4222) as port:
            client = await nats_login(port, passwords)
            publisher = EventPublisher(
                client.jetstream(), producer="alarm", breaker=events_breaker()
            )
            await deliver_after_outage(engine, publisher)
            stored = await stored_event_ids(client)
            await client.close()
        expected = [first, crashed, *many, late]
        check(
            sorted(stored) == sorted(expected),
            "JetStream holds each committed event exactly once, and no rolled-back one",
        )
    finally:
        await engine.dispose()


def main() -> int:
    password = secrets.token_urlsafe(32)
    passwords = read_passwords(NATS_SECRETS)
    code = 0
    try:
        create_database(password)
        with port_forward(f"pod/{POD}", NAMESPACE, 5432) as db_port:
            asyncio.run(run(db_port, password, passwords))
    except (RuntimeError, OSError, NatsError, SQLAlchemyError, EventsUnavailableError) as error:
        # The type and message only. A SQL error could quote a statement, but holds no password.
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        code = 1
    finally:
        kubectl("scale", NATS_STATEFULSET, "--replicas=1")
        kubectl("rollout", "status", NATS_STATEFULSET, "--timeout=180s", required=False)
        with port_forward(f"pod/{NATS_POD}", NAMESPACE, 4222) as port:
            asyncio.run(clean_up_events(port, passwords))
        remove_database()
    if code == 0:
        print("Outbox check passed")
    return code


if __name__ == "__main__":
    sys.exit(main())
