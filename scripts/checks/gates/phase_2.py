#!/usr/bin/env python3
"""Check the Phase 2 exit gate on the real local cluster.

SRS section 12: an event survives a pod restart, and a redelivered audit event creates only one
record. Run `make cluster` first. The system is left running and healthy, without the test records.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import grpc
from nats.errors import Error as NatsError
from nats.js.errors import APIError

from scripts.checks.data.audit import (
    APPLICATION,
    DEPLOYMENT,
    NATS_POD,
    RUN,
    SIGNING_KEY_FILE,
    AuditApi,
    check,
    clean_up_database,
    clean_up_events,
    kubectl,
    nats_client,
    request_record,
    restarts,
)
from scripts.setup.gateway_routes import port_forward
from scripts.setup.nats_access import SECRETS_FILE as NATS_SECRETS
from scripts.setup.provision_postgres import NAMESPACE as DATA
from scripts.setup.provision_postgres import read_passwords

REPOSITORY = Path(__file__).resolve().parents[3]
NATS_STATEFULSET = "statefulset/windops-nats"
AUDIT_STREAM = "WINDOPS_AUDIT"
CONSUMER = "audit"
BOOTSTRAP_TARGETS = ("images", "secrets", "infra", "migrate", "services")
WORKLOADS = (
    ("statefulset/windops-postgres", DATA),
    ("statefulset/windops-qdrant", DATA),
    (NATS_STATEFULSET, DATA),
    ("deployment/windops-redis", DATA),
    (DEPLOYMENT, APPLICATION),
)


def bootstrap() -> None:
    """Deploy with the documented commands, in the order SRS 11.4 sets."""
    for target in BOOTSTRAP_TARGETS:
        result = subprocess.run(
            ["make", "--no-print-directory", target],
            cwd=REPOSITORY, capture_output=True, text=True, check=False,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(f"make {target} failed:\n{result.stderr[-1500:]}")
    for workload, namespace in WORKLOADS:
        kubectl("rollout", "status", workload, f"--namespace={namespace}", "--timeout=300s")
    print("The documented commands deployed the data stores and the Audit Service", flush=True)


def stop_audit() -> None:
    kubectl("scale", DEPLOYMENT, "--replicas=0", f"--namespace={APPLICATION}")
    kubectl(
        "wait", "pod", "--selector=app.kubernetes.io/name=windops-audit", "--for=delete",
        f"--namespace={APPLICATION}", "--timeout=120s",
    )  # fmt: skip


def start_audit() -> None:
    kubectl("scale", DEPLOYMENT, "--replicas=1", f"--namespace={APPLICATION}")
    kubectl("rollout", "status", DEPLOYMENT, f"--namespace={APPLICATION}", "--timeout=240s")


def nats_pod_uid() -> str:
    return kubectl(
        "get", "pod", NATS_POD, f"--namespace={DATA}", "--output=jsonpath={.metadata.uid}"
    )  # fmt: skip


def restart_nats() -> None:
    """Delete the pod, so the event can only survive through its persistent volume."""
    before = nats_pod_uid()
    kubectl("delete", "pod", NATS_POD, f"--namespace={DATA}")
    kubectl("rollout", "status", NATS_STATEFULSET, f"--namespace={DATA}", "--timeout=180s")
    kubectl(
        "wait", f"pod/{NATS_POD}", "--for=condition=Ready", f"--namespace={DATA}", "--timeout=180s"
    )
    check(nats_pod_uid() != before, "The NATS pod was replaced by a new one")


async def publish(
    port: int, passwords: dict[str, str], event_id: str, message_id: str = ""
) -> None:
    alarm = await nats_client(port, passwords, "alarm")
    await request_record(alarm, event_id=event_id, message_id=message_id or None)
    await alarm.close()


async def stored_count(port: int, passwords: dict[str, str]) -> int:
    admin = await nats_client(port, passwords, None)
    messages = (await admin.jetstream().stream_info(AUDIT_STREAM)).state.messages
    await admin.close()
    return int(messages)


async def delete_consumer(port: int, passwords: dict[str, str]) -> None:
    """Forget what the consumer has acknowledged, so JetStream delivers every message again."""
    admin = await nats_client(port, passwords, None)
    await admin.jetstream().delete_consumer(AUDIT_STREAM, CONSUMER)
    await admin.close()


async def delivered_by_consumer(port: int, passwords: dict[str, str]) -> int:
    """Return how many messages the current consumer has been delivered, from JetStream itself."""
    admin = await nats_client(port, passwords, None)
    info = await admin.jetstream().consumer_info(AUDIT_STREAM, CONSUMER)
    await admin.close()
    return int(info.delivered.consumer_seq or 0)


async def dead_letters(port: int, passwords: dict[str, str]) -> int:
    admin = await nats_client(port, passwords, None)
    js = admin.jetstream()
    reader = await js.pull_subscribe("deadletter.audit", stream="WINDOPS_DEAD_LETTER")
    found = 0
    try:
        while True:
            try:
                messages = await reader.fetch(100, timeout=1)
            except TimeoutError:
                return found
            for message in messages:
                found += RUN.encode() in message.data
                await message.ack()
    finally:
        await reader.unsubscribe()
        await admin.close()


async def read_records(secret: str) -> list[str]:
    with port_forward(DEPLOYMENT, APPLICATION, 50051) as port:
        api = AuditApi(port, secret)
        try:
            return [record.event_id for record in await api.records()]
        finally:
            await api.close()


async def wait_for_record(secret: str, event_id: str) -> None:
    with port_forward(DEPLOYMENT, APPLICATION, 50051) as port:
        api = AuditApi(port, secret)
        try:
            await api.wait_for(event_id, attempts=120)
        finally:
            await api.close()


async def chain_is_valid(secret: str) -> tuple[bool, int]:
    with port_forward(DEPLOYMENT, APPLICATION, 50051) as port:
        api = AuditApi(port, secret)
        try:
            result = await api.verify()
            return bool(result.valid), int(result.records_checked)
        finally:
            await api.close()


def check_event_survives_restart(passwords: dict[str, str], secret: str) -> str:
    """With no consumer to take it, the event exists only in JetStream's storage."""
    event_id = f"survives-{RUN}"
    stop_audit()
    print("The Audit Service is stopped, so nothing can consume the event", flush=True)
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        before = asyncio.run(stored_count(port, passwords))
        asyncio.run(publish(port, passwords, event_id))
        after = asyncio.run(stored_count(port, passwords))
    check(after == before + 1, "JetStream acknowledged and stored the event")
    restart_nats()
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        check(
            asyncio.run(stored_count(port, passwords)) == after,
            "The stream still holds the event after the restart",
        )
    start_audit()
    asyncio.run(wait_for_record(secret, event_id))
    check(True, "The Audit Service received the event, and made a record of it")
    return event_id


def check_redelivery_past_jetstream(passwords: dict[str, str], secret: str, event_id: str) -> None:
    """A new message ID gets past JetStream's duplicate check, so only the service can catch it."""
    marker = f"after-repeat-{RUN}"
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        asyncio.run(publish(port, passwords, event_id, message_id=f"{event_id}-again"))
        asyncio.run(publish(port, passwords, marker))
    asyncio.run(wait_for_record(secret, marker))
    records = asyncio.run(read_records(secret))
    check(
        records.count(event_id) == 1,
        "The same event sent again, past JetStream's duplicate check, created no second record",
    )


def check_real_redelivery(passwords: dict[str, str], secret: str, expected: list[str]) -> None:
    """Make JetStream itself redeliver everything, as it would after losing acknowledgements."""
    before = asyncio.run(read_records(secret))
    stop_audit()
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        stored = asyncio.run(stored_count(port, passwords))
        asyncio.run(delete_consumer(port, passwords))
    print("The consumer was deleted, so JetStream redelivers every stored request", flush=True)
    start_audit()
    marker = f"after-redelivery-{RUN}"
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        asyncio.run(publish(port, passwords, marker))
    # The marker follows the redelivered messages, so its record means they have been handled.
    asyncio.run(wait_for_record(secret, marker))
    # Without this, a consumer that skipped the old messages would pass everything below.
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        delivered = asyncio.run(delivered_by_consumer(port, passwords))
    check(
        stored > 0 and delivered >= stored + 1,
        f"JetStream really redelivered: {delivered} deliveries, for {stored} stored and 1 new",
    )
    after = asyncio.run(read_records(secret))
    check(
        [item for item in after if item != marker] == before,
        f"All {len(before)} of this tenant's records are unchanged, with no duplicates",
    )
    for event_id in expected:
        check(after.count(event_id) == 1, f"{event_id.removesuffix(RUN)}: exactly one record")


def check_chain_and_health(passwords: dict[str, str], secret: str) -> None:
    valid, checked = asyncio.run(chain_is_valid(secret))
    check(valid and checked > 0, f"The hash chain verifies across {checked} records")
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        found = asyncio.run(dead_letters(port, passwords))
    check(found == 0, "Nothing from this run reached the dead-letter stream")
    # Its pod was created when the last stage started it, so any restart here is a crash.
    check(restarts() == 0, "The Audit Service did not crash")


def main() -> int:
    secret = read_passwords(SIGNING_KEY_FILE)["signing-key"]
    passwords = read_passwords(NATS_SECRETS)
    code = 0
    try:
        bootstrap()
        first = check_event_survives_restart(passwords, secret)
        check_redelivery_past_jetstream(passwords, secret, first)
        check_real_redelivery(passwords, secret, [first, f"after-repeat-{RUN}"])
        check_chain_and_health(passwords, secret)
    except (
        RuntimeError,
        OSError,
        NatsError,
        APIError,
        TimeoutError,
        grpc.aio.AioRpcError,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        code = 1
    finally:
        kubectl("scale", DEPLOYMENT, "--replicas=1", f"--namespace={APPLICATION}", required=False)
        clean_up_database()
        with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
            asyncio.run(clean_up_events(port, passwords))
    if code == 0:
        print("The Phase 2 exit gate passes.")
    return code


if __name__ == "__main__":
    sys.exit(main())
