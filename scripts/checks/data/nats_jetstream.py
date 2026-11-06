#!/usr/bin/env python3
"""Prove JetStream works on the real local cluster, including an outage.

Run `make secrets` and `make infra` first. This restarts NATS and stops it for a moment. Stored
events and consumer positions survive that. Its test events and consumers are removed afterwards.
"""

import asyncio
import contextlib
import subprocess
import sys
import uuid

import nats
from nats.aio.client import Client
from nats.errors import Error as NatsError
from nats.errors import NoServersError
from nats.js.errors import APIError, NotFoundError
from windops.events.v1 import events_pb2
from windops_events import (
    DEAD_LETTER_STREAM,
    STREAMS,
    Event,
    EventConsumer,
    EventPublisher,
    EventsUnavailableError,
    build_envelope,
    connect,
    events_breaker,
    tls_context,
)
from windops_resilience import BreakerConfig, BreakerState, CircuitBreaker

from scripts.lib.tls_client import authority_file
from scripts.setup.gateway_routes import port_forward
from scripts.setup.nats_access import ADMIN_USER, NAMESPACE, SECRETS_FILE
from scripts.setup.provision_postgres import ADMIN_KEY, CONTEXT, database_name, read_passwords

POD = "windops-nats-0"
STATEFULSET = "statefulset/windops-nats"
EVENTS_STREAM = "WINDOPS_EVENTS"
TENANT = "nats-check"
RUN = uuid.uuid4().hex[:8]
ALARM = events_pb2.AlarmChanged(alarm_id=f"check-{RUN}", turbine_id="check")
APPROVAL = events_pb2.ApprovalDecided(approval_id=f"check-{RUN}")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={NAMESPACE}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def wait_ready() -> None:
    kubectl("rollout", "status", STATEFULSET, "--timeout=180s")
    kubectl("wait", f"pod/{POD}", "--for=condition=Ready", "--timeout=180s")


class Run:
    """Connections for one port-forward, and what must be removed afterwards."""

    def __init__(self, port: int, passwords: dict[str, str]) -> None:
        self.url = f"tls://127.0.0.1:{port}"
        self.passwords = passwords
        self.clients: list[Client] = []

    async def login(self, service: str) -> Client:
        client = await connect(
            url=self.url,
            username=database_name(service),
            password=self.passwords[service],
            name=f"check-{service}",
            ca_file=authority_file(),
        )
        self.clients.append(client)
        return client

    async def admin(self) -> Client:
        client = await connect(
            url=self.url,
            username=ADMIN_USER,
            password=self.passwords[ADMIN_KEY],
            name="check-admin",
            ca_file=authority_file(),
        )
        self.clients.append(client)
        return client

    async def close(self) -> None:
        for client in self.clients:
            await client.close()


async def refused(url: str, **login: str) -> bool:
    try:
        client = await nats.connect(
            servers=[url],
            # With the right CA, so a refusal below is about the login and not the certificate.
            tls=tls_context(authority_file()),
            connect_timeout=2,
            max_reconnect_attempts=0,
            allow_reconnect=False,
            **login,
        )
    except (NatsError, OSError, TimeoutError):
        return True
    await client.close()
    return False


async def check_logins(run: Run) -> None:
    check(await refused(run.url), "a connection with no login is refused")
    check(
        await refused(run.url, user=database_name("alarm"), password=uuid.uuid4().hex),
        "a wrong password is refused",
    )


async def check_streams(admin: Client) -> None:
    js = admin.jetstream()
    for spec in STREAMS:
        config = (await js.stream_info(spec.name)).config
        check(
            set(config.subjects or []) == set(spec.subjects)
            and str(config.storage) in ("file", "StorageType.FILE")
            and config.max_age == spec.max_age_seconds
            and config.max_bytes == spec.max_bytes,
            f"{spec.name} exists, stores to disk, and has its retention limits",
        )


async def check_permissions(run: Run) -> None:
    alarm = await run.login("alarm")
    publisher = EventPublisher(alarm.jetstream(), producer="alarm", breaker=events_breaker())
    stored = await publisher.publish("alarm.changed", tenant_id=TENANT, payload=ALARM)
    check(stored.stream == EVENTS_STREAM, "a service can publish a subject it owns")

    admin = await run.admin()
    js = admin.jetstream()
    before = (await js.stream_info(EVENTS_STREAM)).state.messages
    forged = False
    try:
        await publisher.publish("approval.decided", tenant_id=TENANT, payload=APPROVAL)
        forged = True
    except EventsUnavailableError:
        pass
    after = (await js.stream_info(EVENTS_STREAM)).state.messages
    check(
        not forged and before == after,
        "Alarm cannot forge an approval.decided event, so only Approval can approve",
    )

    service_js = alarm.jetstream()
    for name, attempt in (
        ("delete", service_js.delete_stream(EVENTS_STREAM)),
        ("purge", service_js.purge_stream(EVENTS_STREAM)),
    ):
        allowed = False
        try:
            await attempt
            allowed = True
        except (NatsError, APIError, TimeoutError):
            pass
        check(not allowed, f"a service cannot {name} a stream")
    remaining = (await js.stream_info(EVENTS_STREAM)).state.messages
    check(remaining == after, "the stream is intact after those attempts")


async def check_duplicates(run: Run) -> None:
    alarm = await run.login("alarm")
    publisher = EventPublisher(alarm.jetstream(), producer="alarm", breaker=events_breaker())
    event_id = f"check-duplicate-{RUN}"
    first = await publisher.publish(
        "alarm.changed", tenant_id=TENANT, payload=ALARM, event_id=event_id
    )
    second = await publisher.publish(
        "alarm.changed", tenant_id=TENANT, payload=ALARM, event_id=event_id
    )
    check(
        not first.duplicate and second.duplicate and first.sequence == second.sequence,
        "a repeated event is stored only once (EVT-003)",
    )


async def drain(consumer: EventConsumer, *, rounds: int = 8) -> None:
    for _ in range(rounds):
        await consumer.run_once(batch=20, timeout=0.5)


async def check_retry_and_dead_letter(run: Run) -> None:
    fault = await run.login("fault-detection")
    alarm = await run.login("alarm")
    js = fault.jetstream()
    publisher = EventPublisher(alarm.jetstream(), producer="alarm", breaker=events_breaker())
    durable = f"check-{RUN}"
    target = f"retry-{RUN}"
    flaky = f"flaky-{RUN}"
    attempts: dict[str, int] = {}
    tenants: list[str] = []

    async def handler(event: Event) -> None:
        tenants.append(event.tenant_id)
        attempts[event.event_id] = attempts.get(event.event_id, 0) + 1
        if event.event_id == target:
            raise RuntimeError("always fails")
        if event.event_id == flaky and attempts[flaky] == 1:
            raise RuntimeError("fails once")

    consumer = EventConsumer(
        js,
        subject="alarm.changed",
        durable=durable,
        handler=handler,
        max_attempts=3,
        retry_delay_seconds=0.2,
    )
    await consumer.start()
    await publisher.publish("alarm.changed", tenant_id=TENANT, payload=ALARM, event_id=flaky)
    await publisher.publish("alarm.changed", tenant_id=TENANT, payload=ALARM, event_id=target)

    # No tenant: the library refuses to build this, so publish the raw bytes a buggy service might.
    envelope = build_envelope("alarm.changed", tenant_id=TENANT, payload=ALARM, producer="alarm")
    envelope.tenant_id = ""
    envelope.event_id = f"no-tenant-{RUN}"
    await alarm.jetstream().publish(
        "alarm.changed",
        envelope.SerializeToString(),
        headers={"Nats-Msg-Id": envelope.event_id},
    )
    await drain(consumer)
    check(attempts.get(flaky) == 2, "a failed event is delivered again, and then succeeds")
    check(attempts.get(target) == 3, "a failing event stops after its attempt limit")
    check(
        f"no-tenant-{RUN}" not in attempts and all(tenants),
        "an event with no tenant never reaches the handler (DAT-007)",
    )

    admin = await run.admin()
    letters = await admin.jetstream().pull_subscribe(
        f"deadletter.{durable}", durable=f"letters-{RUN}", stream=DEAD_LETTER_STREAM
    )
    messages = await letters.fetch(10, timeout=5)
    reasons = sorted((message.headers or {}).get("Windops-Reason", "") for message in messages)
    check(
        reasons == ["failed", "invalid"],
        "the failing and invalid events are in the dead-letter stream (EVT-003)",
    )
    for message in messages:
        await message.ack()
    await drain(consumer, rounds=2)
    check(attempts.get(flaky) == 2, "a handled event is not delivered again")


async def publish_for_restart(run: Run) -> str:
    alarm = await run.login("alarm")
    publisher = EventPublisher(alarm.jetstream(), producer="alarm", breaker=events_breaker())
    event_id = f"restart-{RUN}"
    await publisher.publish("alarm.changed", tenant_id=TENANT, payload=ALARM, event_id=event_id)
    return event_id


async def check_after_restart(run: Run, event_id: str) -> None:
    fault = await run.login("fault-detection")
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event.event_id)

    consumer = EventConsumer(
        fault.jetstream(), subject="alarm.changed", durable=f"check-{RUN}", handler=handler
    )
    await consumer.start()
    await drain(consumer, rounds=4)
    check(seen == [event_id], "an event survives a pod restart, and is delivered once (SRS 12)")


async def check_outage(passwords: dict[str, str]) -> None:
    breaker = CircuitBreaker("jetstream", BreakerConfig(slow_call_seconds=5.0, open_seconds=3.0))
    with port_forward(f"pod/{POD}", NAMESPACE, 4222) as port:
        run = Run(port, passwords)
        alarm = await run.login("alarm")
        publisher = EventPublisher(alarm.jetstream(), producer="alarm", breaker=breaker)
        await publisher.publish(
            "alarm.changed", tenant_id=TENANT, payload=ALARM, event_id=f"before-outage-{RUN}"
        )
        kubectl("scale", STATEFULSET, "--replicas=0")
        kubectl("wait", f"pod/{POD}", "--for=delete", "--timeout=120s")
        reported = 0
        for _ in range(12):
            try:
                await publisher.publish("alarm.changed", tenant_id=TENANT, payload=ALARM)
            except EventsUnavailableError:
                reported += 1
        check(reported == 12, "publishing reports an outage, so the outbox can keep the event")
        check(breaker.state is BreakerState.OPEN, "the outage opens the circuit breaker")
        await run.close()
    kubectl("scale", STATEFULSET, "--replicas=1")
    wait_ready()
    await asyncio.sleep(3.5)
    with port_forward(f"pod/{POD}", NAMESPACE, 4222) as port:
        run = Run(port, passwords)
        alarm = await run.login("alarm")
        publisher = EventPublisher(alarm.jetstream(), producer="alarm", breaker=breaker)
        for index in range(5):
            await publisher.publish(
                "alarm.changed",
                tenant_id=TENANT,
                payload=ALARM,
                event_id=f"after-outage-{RUN}-{index}",
            )
        check(
            breaker.state is BreakerState.CLOSED, "publishing works again, and the breaker closes"
        )
        await run.close()


async def clean_up(port: int, passwords: dict[str, str]) -> None:
    """Remove this run's consumers and events, and nothing else."""
    run = Run(port, passwords)
    admin = await run.admin()
    js = admin.jetstream()
    try:
        for stream, name in (
            (EVENTS_STREAM, f"check-{RUN}"),
            (DEAD_LETTER_STREAM, f"letters-{RUN}"),
        ):
            with contextlib.suppress(NotFoundError):
                await js.delete_consumer(stream, name)
        for stream in (EVENTS_STREAM, DEAD_LETTER_STREAM):
            await delete_tenant_messages(js, stream)
    finally:
        await run.close()


async def delete_tenant_messages(js: nats.js.JetStreamContext, stream: str) -> None:
    subject = "alarm.changed" if stream == EVENTS_STREAM else "deadletter.>"
    reader = await js.pull_subscribe(subject, stream=stream)
    try:
        while True:
            try:
                messages = await reader.fetch(100, timeout=1)
            except TimeoutError:
                return
            for message in messages:
                sequence = message.metadata.sequence.stream if message.metadata.sequence else 0
                if RUN.encode() in message.data or RUN in str(message.headers):
                    await js.delete_msg(stream, sequence)
                await message.ack()
    finally:
        await reader.unsubscribe()


async def run_before_restart(port: int, passwords: dict[str, str]) -> str:
    run = Run(port, passwords)
    try:
        await check_logins(run)
        await check_streams(await run.admin())
        await check_permissions(run)
        await check_duplicates(run)
        await check_retry_and_dead_letter(run)
        return await publish_for_restart(run)
    finally:
        await run.close()


async def run_after_restart(port: int, passwords: dict[str, str], event_id: str) -> None:
    run = Run(port, passwords)
    try:
        await check_after_restart(run, event_id)
    finally:
        await run.close()


def main() -> int:
    if not SECRETS_FILE.exists():
        print("No NATS passwords found. Run: make secrets", file=sys.stderr)
        return 1
    passwords = read_passwords(SECRETS_FILE)
    code = 0
    try:
        wait_ready()
        with port_forward(f"pod/{POD}", NAMESPACE, 4222) as port:
            event_id = asyncio.run(run_before_restart(port, passwords))
        kubectl("delete", "pod", POD)
        wait_ready()
        with port_forward(f"pod/{POD}", NAMESPACE, 4222) as port:
            asyncio.run(run_after_restart(port, passwords, event_id))
        asyncio.run(check_outage(passwords))
    except (RuntimeError, OSError, NatsError, APIError, NoServersError, TimeoutError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        code = 1
    finally:
        kubectl("scale", STATEFULSET, "--replicas=1")
        wait_ready()
        with port_forward(f"pod/{POD}", NAMESPACE, 4222) as port:
            asyncio.run(clean_up(port, passwords))
    if code == 0:
        print("JetStream check passed")
    return code


if __name__ == "__main__":
    sys.exit(main())
