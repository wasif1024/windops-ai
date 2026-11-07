"""Make device credentials for the demo turbines, which nobody could otherwise make. Safe to repeat.

Run inside the service's pod, with its own database login, by `make telemetry-devices`:

    python -m windops_telemetry.devices north-sea:dogger-east-t01 north-sea:dogger-east-t02 ...

For each tenant and turbine with no working credential, it makes one, and prints one line of JSON
with its token, for the simulator's Secret. A turbine that has one is skipped, so its token is
not shown again. Pass --replace to revoke the old one and make a new one.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine
from windops_migrations import database_url, in_tenant

from windops_telemetry import credentials
from windops_telemetry.store import DEVICE_CREDENTIALS, audit

ACTOR = "bootstrap"


async def make(pairs: list[tuple[str, str]], replace: bool) -> list[dict[str, str]]:
    engine = create_async_engine(database_url())
    made = []
    try:
        for tenant, turbine in pairs:
            now = datetime.now(UTC)
            async with engine.begin() as connection:
                await in_tenant(connection, tenant)
                working = DEVICE_CREDENTIALS.c.revoked_at.is_(None)
                mine = (DEVICE_CREDENTIALS.c.tenant_id == tenant) & (
                    DEVICE_CREDENTIALS.c.turbine_id == turbine
                )
                if replace:
                    await connection.execute(
                        update(DEVICE_CREDENTIALS).where(mine, working).values(revoked_at=now)
                    )
                elif (
                    await connection.execute(
                        select(DEVICE_CREDENTIALS.c.device_id).where(mine, working)
                    )
                ).first():
                    continue
                device_id, secret = credentials.new_device_id(), credentials.new_secret()
                await connection.execute(
                    DEVICE_CREDENTIALS.insert().values(
                        tenant_id=tenant, device_id=device_id, turbine_id=turbine,
                        secret_hash=credentials.hashed(secret), created_by=ACTOR, created_at=now,
                    )
                )  # fmt: skip
                await audit(
                    connection, tenant_id=tenant, actor_id=ACTOR, actor_type="service",
                    action="telemetry.device_credential.created", resource_id=device_id,
                )  # fmt: skip
            made.append(
                {
                    "tenant_id": tenant,
                    "turbine_id": turbine,
                    "token": credentials.token(tenant, device_id, secret),
                }
            )
    finally:
        await engine.dispose()
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("turbines", nargs="+", help="tenant:turbine")
    parser.add_argument("--replace", action="store_true")
    arguments = parser.parse_args()
    pairs = []
    for item in arguments.turbines:
        tenant, _, turbine = item.partition(":")
        if not credentials.TOKEN.match(f"{tenant}.dev_0000000000000000.{'a' * 43}") or not turbine:
            print(f"Not tenant:turbine: {item}", file=sys.stderr)
            return 2
        pairs.append((tenant, turbine))
    for line in asyncio.run(make(pairs, arguments.replace)):
        print(json.dumps(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
