#!/usr/bin/env python3
"""List the messages in the dead-letter stream, without their contents.

make dead-letters
"""

import asyncio
import sys

from gateway_routes import port_forward
from nats.errors import Error as NatsError
from nats_access import ADMIN_USER, NAMESPACE, SECRETS_FILE
from provision_postgres import ADMIN_KEY, read_passwords
from windops_events import DEAD_LETTER_PREFIX, DEAD_LETTER_STREAM, connect

POD = "windops-nats-0"
PAGE = 100


def describe(headers: dict[str, str], stored_at: object) -> str:
    """Return one line about a message. Contents can hold tenant data, so stay out (OBS-003)."""
    return (
        f"{stored_at}  consumer={headers.get('Windops-Consumer', '?')}  "
        f"subject={headers.get('Windops-Original-Subject', '?')}  "
        f"reason={headers.get('Windops-Reason', '?')}  "
        f"error={headers.get('Windops-Error-Type', '?')}"
    )


async def list_letters(port: int, password: str) -> int:
    client = await connect(
        url=f"nats://127.0.0.1:{port}", username=ADMIN_USER, password=password, name="dead-letters"
    )
    count = 0
    try:
        js = client.jetstream()
        reader = await js.pull_subscribe(f"{DEAD_LETTER_PREFIX}.>", stream=DEAD_LETTER_STREAM)
        while True:
            try:
                messages = await reader.fetch(PAGE, timeout=1)
            except TimeoutError:
                break
            for message in messages:
                print(describe(message.headers or {}, message.metadata.timestamp))
                count += 1
                await message.ack()
        await reader.unsubscribe()
    finally:
        await client.close()
    return count


def main() -> int:
    if not SECRETS_FILE.exists():
        print("No NATS passwords found. Run: make secrets", file=sys.stderr)
        return 1
    password = read_passwords(SECRETS_FILE)[ADMIN_KEY]
    try:
        with port_forward(f"pod/{POD}", NAMESPACE, 4222) as port:
            count = asyncio.run(list_letters(port, password))
    except (NatsError, TimeoutError, OSError, RuntimeError) as error:
        print(f"Could not read the dead-letter stream: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"{count} dead letters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
