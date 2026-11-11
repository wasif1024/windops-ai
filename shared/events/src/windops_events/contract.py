"""What every events back end must do, as one set of scenarios (ADR 0006).

The unit tests run these against NATS and Service Bus stand-ins, and `make servicebus-check` runs
them against a real Service Bus namespace, so the two back ends stay equivalent:

1. An event is delivered to its consumer, and once handled is not delivered again.
2. The same event sent twice, as the outbox may, is delivered once.
3. A handler that keeps failing has the event dead-lettered after its attempts, as `failed`.
4. Something that is not an event is dead-lettered at once, as `invalid`.
5. A handler whose own dependency is down is asked again later, and never dead-letters.

Each scenario is its own producer, asking on its own audit.requested subject, as a service may ask
only on its own (EVT-002). So runs, and scenarios, never see each other's events.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from windops.events.v1 import events_pb2

from windops_events.backend import Consumer, Handler, Publisher
from windops_events.client import RetryLaterError
from windops_events.envelope import Event, build_envelope
from windops_events.streams import audit_request_subject


@dataclass
class Harness:
    """One back end, as the scenarios need to see it."""

    # A publisher for one producer, which may ask for audit records on its own subject only.
    publisher: Callable[[str], Publisher]
    # subject, durable, handler, max_attempts
    consumer: Callable[[str, str, Handler, int], Consumer]
    durable: str
    # Sends bytes that are not an event, on a subject.
    publish_raw: Callable[[str, bytes], Awaitable[None]]
    # The reasons of the dead letters that came from a subject.
    dead_letters: Callable[[str], Awaitable[list[str]]]
    # How long to keep reading for a redelivery: a lock or a retry delay.
    patience_seconds: float = 2.0


@dataclass(frozen=True)
class Scene:
    producer: str

    @property
    def subject(self) -> str:
        return audit_request_subject(self.producer)


def _scene(run: str, name: str) -> Scene:
    return Scene(f"c{run}{name}")


def _payload() -> events_pb2.AuditRequested:
    return events_pb2.AuditRequested(
        action="contract.check", actor_type="service", actor_id="contract",
        resource_type="check", resource_id="contract", outcome="succeeded",
    )  # fmt: skip


async def _drive(consumer: Consumer, until: Callable[[], bool], seconds: float) -> None:
    await consumer.start()
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        await consumer.run_once(batch=10, timeout=1.0)


def _recording(subject: str, into: list[str]) -> Handler:
    async def handler(event: Event) -> None:
        if event.subject == subject:
            into.append(event.event_id)

    return handler


async def delivered_once(h: Harness, run: str) -> None:
    scene = _scene(run, "once")
    seen: list[str] = []
    sent = await h.publisher(scene.producer).publish(
        scene.subject, tenant_id="contract", payload=_payload()
    )
    consumer = h.consumer(scene.subject, h.durable, _recording(scene.subject, seen), 5)
    await _drive(consumer, lambda: bool(seen), 30)
    await _drive(consumer, lambda: False, h.patience_seconds)
    if seen != [sent.event_id]:
        raise AssertionError(f"Expected one delivery of {sent.event_id}, got {seen}")


async def duplicate_delivered_once(h: Harness, run: str) -> None:
    scene = _scene(run, "twice")
    envelope = build_envelope(
        scene.subject, tenant_id="contract", payload=_payload(), producer=scene.producer
    )
    seen: list[str] = []
    publisher = h.publisher(scene.producer)
    await publisher.publish_envelope(scene.subject, envelope)
    await publisher.publish_envelope(scene.subject, envelope)
    consumer = h.consumer(scene.subject, h.durable, _recording(scene.subject, seen), 5)
    await _drive(consumer, lambda: bool(seen), 30)
    await _drive(consumer, lambda: False, h.patience_seconds)
    if seen != [envelope.event_id]:
        raise AssertionError(f"Expected the duplicate to arrive once, got {seen}")


async def failing_is_dead_lettered(h: Harness, run: str) -> None:
    scene = _scene(run, "fails")
    tries: list[str] = []

    async def handler(event: Event) -> None:
        if event.subject == scene.subject:
            tries.append(event.event_id)
            raise RuntimeError("the handler fails")

    await h.publisher(scene.producer).publish(
        scene.subject, tenant_id="contract", payload=_payload()
    )
    consumer = h.consumer(scene.subject, h.durable, handler, 3)
    reasons: list[str] = []
    deadline = time.monotonic() + 60 + 3 * h.patience_seconds
    await consumer.start()
    while time.monotonic() < deadline and not reasons:
        await consumer.run_once(batch=10, timeout=1.0)
        reasons[:] = await h.dead_letters(scene.subject)
    if len(tries) != 3 or reasons != ["failed"]:
        raise AssertionError(f"Expected 3 tries, then a dead letter: {tries}, {reasons}")


async def invalid_is_dead_lettered(h: Harness, run: str) -> None:
    scene = _scene(run, "invalid")

    async def handler(event: Event) -> None:
        if event.subject == scene.subject:
            raise AssertionError("An invalid message reached the handler")

    await h.publish_raw(scene.subject, b"not an event")
    consumer = h.consumer(scene.subject, h.durable, handler, 5)
    reasons: list[str] = []
    deadline = time.monotonic() + 30
    await consumer.start()
    while time.monotonic() < deadline and not reasons:
        await consumer.run_once(batch=10, timeout=1.0)
        reasons[:] = await h.dead_letters(scene.subject)
    if reasons != ["invalid"]:
        raise AssertionError(f"Expected one 'invalid' dead letter, got {reasons}")


async def retry_later_never_dead_letters(h: Harness, run: str) -> None:
    scene = _scene(run, "later")
    tries: list[str] = []

    async def handler(event: Event) -> None:
        if event.subject != scene.subject:
            return
        tries.append(event.event_id)
        if len(tries) < 3:
            raise RetryLaterError("the handler's database is down")

    await h.publisher(scene.producer).publish(
        scene.subject, tenant_id="contract", payload=_payload()
    )
    # One attempt only: a failure would dead-letter it at once, so only RetryLater can pass.
    consumer = h.consumer(scene.subject, h.durable, handler, 1)
    await _drive(consumer, lambda: len(tries) >= 3, 30 + 3 * h.patience_seconds)
    if len(tries) != 3 or await h.dead_letters(scene.subject):
        raise AssertionError(f"Expected 3 tries and no dead letter, got {tries}")


SCENARIOS: tuple[tuple[str, Callable[[Harness, str], Awaitable[None]]], ...] = (
    ("An event is delivered to its consumer once", delivered_once),
    ("The same event sent twice is delivered once", duplicate_delivered_once),
    ("A handler that keeps failing has the event dead-lettered, as failed",
     failing_is_dead_lettered),
    ("Something that is not an event is dead-lettered at once, as invalid",
     invalid_is_dead_lettered),
    ("A handler whose dependency is down is asked again, never dead-lettered",
     retry_later_never_dead_letters),
)  # fmt: skip


async def run_contract(h: Harness, report: Callable[[str], None] = lambda _: None) -> None:
    """Run every scenario on one back end, reporting each as it passes."""
    run = uuid.uuid4().hex[:8]
    for name, scenario in SCENARIOS:
        await scenario(h, run)
        report(name)
        await asyncio.sleep(0)
