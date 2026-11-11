"""Choose where events go from the settings, so a service's code is the same everywhere (ADR 0006).

| `WINDOPS_ENVIRONMENT` | Events |
| --- | --- |
| `local`, `ci`, `test` | NATS JetStream, in the cluster |
| `dev`, `staging`, `live` | Azure Service Bus |

`WINDOPS_EVENTS_BACKEND` overrides the choice when it is `nats` or `servicebus`, so a laptop can
be pointed at a real Service Bus to test it. `auto`, the default, follows the environment.

NATS signs in with its own login, from Kubernetes Secrets. Service Bus signs in with no stored
secret: `DefaultAzureCredential` uses Microsoft Entra Workload ID on AKS, and the Azure CLI's
signed-in session on a developer's machine.
"""

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from google.protobuf.message import Message
from nats.errors import Error as NatsError
from nats.js.errors import APIError
from windops.events.v1 import events_pb2
from windops_resilience import CircuitBreaker

from windops_events.client import (
    EventConsumer,
    EventPublisher,
    EventsUnavailableError,
    PublishedEvent,
    connect,
    events_breaker,
)
from windops_events.envelope import Event

NATS, SERVICE_BUS = "nats", "servicebus"
BACKENDS = (NATS, SERVICE_BUS)
AUTO = "auto"
# The Azure environments. `test` is not one: it is the unit tests' own (windops_grpc.settings).
ON_AZURE = frozenset({"dev", "staging", "live"})
Handler = Callable[[Event], Awaitable[None]]


def events_backend(environ: Mapping[str, str] | None = None) -> str:
    """Which back end the settings choose."""
    env = os.environ if environ is None else environ
    chosen = env.get("WINDOPS_EVENTS_BACKEND", AUTO).strip().lower() or AUTO
    if chosen == AUTO:
        return SERVICE_BUS if env.get("WINDOPS_ENVIRONMENT", "local").strip() in ON_AZURE else NATS
    if chosen not in BACKENDS:
        raise ValueError(f"WINDOPS_EVENTS_BACKEND is one of: {AUTO}, {', '.join(BACKENDS)}")
    return chosen


class Publisher(Protocol):
    async def publish(
        self,
        subject: str,
        *,
        tenant_id: str,
        payload: Message,
        event_id: str | None = None,
        trace_parent: str = "",
        request_id: str = "",
    ) -> PublishedEvent: ...

    async def publish_envelope(
        self, subject: str, envelope: events_pb2.EventEnvelope
    ) -> PublishedEvent: ...


class Consumer(Protocol):
    async def start(self) -> None: ...
    async def run_once(self, *, batch: int = 10, timeout: float = 1.0) -> int: ...


@dataclass
class Events:
    """One service's events: what it publishes with, and how it reads a subject."""

    backend: str
    publisher: Publisher
    _consumer: Callable[..., Consumer]
    _close: Callable[[], Awaitable[None]]
    _consumers: list[Any] = field(default_factory=list)

    def consumer(self, *, subject: str, durable: str, handler: Handler, **options: Any) -> Consumer:
        made = self._consumer(subject=subject, durable=durable, handler=handler, **options)
        self._consumers.append(made)
        return made

    async def close(self) -> None:
        for consumer in self._consumers:
            closing = getattr(consumer, "close", None)
            if closing is not None:
                await closing()
        await self._close()


async def _nats(producer: str, breaker: CircuitBreaker, env: Mapping[str, str]) -> Events:
    try:
        client = await connect(
            url=env["WINDOPS_EVENTS_URL"],
            username=env["WINDOPS_EVENTS_USERNAME"],
            password=env["WINDOPS_EVENTS_PASSWORD"],
            name=producer,
            ca_file=f"{env['WINDOPS_TLS_DIR']}/ca.crt" if env.get("WINDOPS_TLS_DIR") else None,
        )
    except (NatsError, APIError, TimeoutError, OSError) as error:
        raise EventsUnavailableError(type(error).__name__) from error
    js = client.jetstream()

    def consumer(**options: Any) -> Consumer:
        return EventConsumer(js, **options)

    return Events(NATS, EventPublisher(js, producer=producer, breaker=breaker), consumer,
                  client.close)  # fmt: skip


async def _service_bus(producer: str, breaker: CircuitBreaker, env: Mapping[str, str]) -> Events:
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus.aio import ServiceBusClient

    from windops_events.servicebus import ServiceBusConsumer, ServiceBusPublisher

    namespace = env.get("WINDOPS_SERVICEBUS_NAMESPACE", "").strip()
    if not namespace.endswith(".servicebus.windows.net"):
        raise ValueError("WINDOPS_SERVICEBUS_NAMESPACE is a namespace's host name, such as "
                         "windops-dev.servicebus.windows.net")  # fmt: skip
    credential = DefaultAzureCredential()
    # A machine whose Python has no root certificates of its own names a bundle here. Verification
    # is never turned off.
    bundle = env.get("SSL_CERT_FILE") or None
    client = ServiceBusClient(namespace, credential, connection_verify=bundle)
    publisher = ServiceBusPublisher(client, producer=producer, breaker=breaker)

    def consumer(**options: Any) -> Consumer:
        options.pop("retry_delay_seconds", None)
        options.pop("ack_wait_seconds", None)
        return ServiceBusConsumer(client, **options)

    async def close() -> None:
        await publisher.close()
        await client.close()
        await credential.close()

    return Events(SERVICE_BUS, publisher, consumer, close)


async def open_events(
    producer: str,
    *,
    breaker: CircuitBreaker | None = None,
    environ: Mapping[str, str] | None = None,
) -> Events:
    """Connect to the back end the settings choose, or raise EventsUnavailableError."""
    env = os.environ if environ is None else environ
    backend = events_backend(env)
    breaker = breaker or events_breaker()
    if backend == SERVICE_BUS:
        return await _service_bus(producer, breaker, env)
    return await _nats(producer, breaker, env)
