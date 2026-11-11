#!/usr/bin/env python3
"""Create the Service Bus topics and subscriptions that events need, or leave them (ADR 0006).

    make servicebus-provision

It uses WINDOPS_SERVICEBUS_NAMESPACE and signs in with the Azure CLI's session (az login), so it
needs the Azure Service Bus Data Owner role on the namespace. Each JetStream stream becomes a
topic, with the same lifetime and duplicate window, and each consumer gets its subscription.
Safe to repeat. The namespace itself is made by the Azure deployment, not here.
"""

import asyncio
import sys

from azure.core.exceptions import AzureError
from azure.identity.aio import AzureCliCredential
from azure.servicebus.aio.management import ServiceBusAdministrationClient
from windops_events.servicebus import SUBSCRIPTIONS, SubscriptionSpec, ensure_topology, topics

from scripts.lib.settings import get
from scripts.lib.tls_client import trust_public_roots


async def provision(namespace: str, extra: tuple[SubscriptionSpec, ...] = ()) -> None:
    credential = AzureCliCredential()
    try:
        async with ServiceBusAdministrationClient(
            namespace, credential, connection_verify=trust_public_roots()
        ) as admin:
            await ensure_topology(admin, SUBSCRIPTIONS + extra)
    finally:
        await credential.close()


def main() -> int:
    namespace = get("WINDOPS_SERVICEBUS_NAMESPACE")
    try:
        asyncio.run(provision(namespace))
    except AzureError as error:
        print(f"Could not provision {namespace}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    names = ", ".join(name for name, _ in topics())
    subscriptions = ", ".join(f"{s.topic}/{s.durable}" for s in SUBSCRIPTIONS)
    print(f"Service Bus {namespace}: topics {names}; subscriptions {subscriptions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
