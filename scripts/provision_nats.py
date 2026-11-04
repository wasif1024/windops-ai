#!/usr/bin/env python3
"""Create or update the JetStream streams. Safe to run repeatedly, and keeps stored events."""

import asyncio
import subprocess
import sys

from gateway_routes import port_forward
from nats.errors import Error as NatsError
from nats.js.errors import APIError
from nats_access import ADMIN_USER, NAMESPACE, SECRETS_FILE
from provision_postgres import ADMIN_KEY, CONTEXT, read_passwords
from windops_events import STREAMS, connect, ensure_streams

STATEFULSET = "statefulset/windops-nats"


async def provision(port: int, password: str) -> None:
    client = await connect(
        url=f"nats://127.0.0.1:{port}", username=ADMIN_USER, password=password, name="provisioning"
    )
    try:
        await ensure_streams(client.jetstream())
    finally:
        await client.close()


def main() -> int:
    if not SECRETS_FILE.exists():
        print("No NATS passwords found. Run: make secrets", file=sys.stderr)
        return 1
    password = read_passwords(SECRETS_FILE)[ADMIN_KEY]
    subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "rollout", "status", STATEFULSET,
            f"--namespace={NAMESPACE}", "--timeout=180s",
        ],
        check=True,
    )  # fmt: skip
    try:
        with port_forward(STATEFULSET, NAMESPACE, 4222) as port:
            asyncio.run(provision(port, password))
    except (NatsError, APIError, TimeoutError, OSError, RuntimeError) as error:
        print(f"Could not provision JetStream: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"Provisioned {len(STREAMS)} streams")
    return 0


if __name__ == "__main__":
    sys.exit(main())
