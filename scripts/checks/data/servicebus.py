#!/usr/bin/env python3
"""Prove events work on a real Azure Service Bus: the same contract NATS passes (ADR 0006).

    make servicebus-check

Run `make servicebus-provision` first. It reaches the namespace in WINDOPS_SERVICEBUS_NAMESPACE as
a service would, through open_events with WINDOPS_EVENTS_BACKEND=servicebus, signed in with the
Azure CLI's session, so it needs the Azure Service Bus Data Owner role. It adds a `contract`
subscription for itself, runs the five scenarios, and removes the subscription at the end. It
takes a few minutes, as Service Bus redelivers a message only when its lock ends.
"""

import asyncio
import contextlib
import sys
import uuid
from typing import Any

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity.aio import AzureCliCredential
from azure.servicebus import ServiceBusMessage, ServiceBusSubQueue
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus.aio.management import ServiceBusAdministrationClient
from windops.events.v1 import events_pb2
from windops_events import Event, events_backend, events_breaker, open_events
from windops_events.backend import Handler
from windops_events.contract import Harness, run_contract
from windops_events.servicebus import (
    LOCK_SECONDS,
    ServiceBusConsumer,
    ServiceBusPublisher,
    SubscriptionSpec,
    topic_for,
)
from windops_events.streams import AUDIT_REQUEST_SUBJECTS, audit_request_subject

from scripts.lib.settings import load
from scripts.setup.provision_servicebus import provision

CONTRACT = SubscriptionSpec(AUDIT_REQUEST_SUBJECTS, "contract")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


async def check_the_switch(settings: dict[str, str], client: Any) -> None:
    """open_events, as a service calls it, reaches Service Bus, and its event arrives."""
    events = await open_events("cswitch", environ=settings)
    try:
        check(events.backend == "servicebus", "open_events connects to Service Bus, from settings")
        sent = await events.publisher.publish(
            audit_request_subject("cswitch"), tenant_id="contract",
            payload=events_pb2.AuditRequested(action="contract.switch", actor_type="service",
                                              actor_id="contract", outcome="succeeded"),
        )  # fmt: skip
    finally:
        await events.close()
    seen: list[str] = []

    async def handler(event: Event) -> None:
        if event.event_id == sent.event_id:
            seen.append(event.event_id)

    consumer = ServiceBusConsumer(client, subject=CONTRACT.subject, durable=CONTRACT.durable,
                                  handler=handler)  # fmt: skip
    await consumer.start()
    for _ in range(30):
        if seen:
            break
        await consumer.run_once(batch=20, timeout=1.0)
    await consumer.close()
    check(seen == [sent.event_id], "and the event it published is delivered to a subscription")


async def run(namespace: str) -> None:
    settings = {**load(), "WINDOPS_EVENTS_BACKEND": "servicebus"}
    check(events_backend(settings) == "servicebus",
          "WINDOPS_EVENTS_BACKEND=servicebus chooses Service Bus")  # fmt: skip
    await provision(namespace, (CONTRACT,))
    check(True, "The topics and subscriptions are provisioned")
    credential = AzureCliCredential()
    client = ServiceBusClient(namespace, credential)

    def publisher(producer: str) -> Any:
        return ServiceBusPublisher(client, producer=producer, breaker=events_breaker())

    def consumer(subject: str, durable: str, handler: Handler, attempts: int) -> Any:
        return ServiceBusConsumer(
            client, subject=subject, durable=CONTRACT.durable, handler=handler,
            max_attempts=attempts,
        )  # fmt: skip

    async def raw(subject: str, data: bytes) -> None:
        async with client.get_topic_sender(topic_for(subject)) as sender:
            await sender.send_messages(
                ServiceBusMessage(data, subject=subject, message_id=uuid.uuid4().hex)
            )

    async def dead_letters(subject: str) -> list[str]:
        async with client.get_subscription_receiver(
            CONTRACT.topic, CONTRACT.durable, sub_queue=ServiceBusSubQueue.DEAD_LETTER
        ) as receiver:
            found = await receiver.peek_messages(max_message_count=250)
        return [str(m.dead_letter_reason) for m in found if m.subject == subject]

    harness = Harness(publisher, consumer, CONTRACT.durable, raw, dead_letters,
                      patience_seconds=LOCK_SECONDS + 5)  # fmt: skip
    try:
        await check_the_switch(settings, client)
        await run_contract(harness, lambda name: check(True, f"Service Bus: {name}"))
    finally:
        await client.close()
        await credential.close()
        cleanup = AzureCliCredential()
        async with ServiceBusAdministrationClient(namespace, cleanup) as admin:
            with contextlib.suppress(ResourceNotFoundError):
                await admin.delete_subscription(CONTRACT.topic, CONTRACT.durable)
        await cleanup.close()


def main() -> int:
    namespace = load().get("WINDOPS_SERVICEBUS_NAMESPACE", "")
    try:
        asyncio.run(run(namespace))
    except (RuntimeError, AssertionError, AzureError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Service Bus check passed: the same contract as NATS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
