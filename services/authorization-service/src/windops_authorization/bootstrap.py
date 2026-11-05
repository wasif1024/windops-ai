"""Give the first administrators their roles, which nobody could otherwise grant. Safe to repeat.

Run inside the service's pod, with its own database login:

    python -m windops_authorization.bootstrap \
        --tenant north-sea --user <id> --role tenant-administrator
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import create_async_engine
from windops_migrations import database_url

from windops_authorization import policy
from windops_authorization.policy import RuleError, Scope
from windops_authorization.store import add_grant, audit, find_active_grant

ACTOR = "bootstrap"


async def bootstrap(tenant: str, user: str, role: str) -> bool:
    """Return True if this added the grant, and False if it already existed."""
    policy.check_grantable(tenant, role, Scope())
    engine = create_async_engine(database_url())
    try:
        async with engine.begin() as connection:
            if await find_active_grant(connection, tenant, user, role, Scope()):
                return False
            await add_grant(
                connection, tenant, user, role, Scope(), actor=ACTOR, now=datetime.now(UTC)
            )
            await audit(
                connection,
                tenant_id=tenant,
                action=f"authorization.role.granted.{role}",
                actor_id=ACTOR,
                actor_type="service",
                resource_type="user",
                resource_id=user,
                outcome="succeeded",
            )
        return True
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--role", required=True, choices=policy.ROLES)
    arguments = parser.parse_args()
    try:
        added = asyncio.run(bootstrap(arguments.tenant, arguments.user, arguments.role))
    except RuleError as error:
        print(error, file=sys.stderr)
        return 2
    print("Granted" if added else "Already granted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
