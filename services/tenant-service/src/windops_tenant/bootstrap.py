"""Record a tenant whose Keycloak organization already exists, such as a demo one. Safe to repeat.

Run inside the service's pod, with its own database login:

    python -m windops_tenant.bootstrap --tenant north-sea --name "North Sea Wind"
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import select
from windops_migrations import create_service_engine, in_tenant

from windops_tenant import branding as rules
from windops_tenant.branding import BrandingError
from windops_tenant.service import add_tenant
from windops_tenant.store import TENANTS, audit

ACTOR = "bootstrap"


async def bootstrap(tenant: str, name: str) -> bool:
    """Return True if this added the tenant, and False if it was already recorded."""
    rules.check_tenant_id(tenant)
    engine = create_service_engine()
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            found = await connection.scalar(
                select(TENANTS.c.tenant_id).where(TENANTS.c.tenant_id == tenant)
            )
            if found is not None:
                return False
            await add_tenant(connection, tenant, name, actor=ACTOR, now=datetime.now(UTC))
            await audit(
                connection,
                tenant_id=tenant,
                action="tenant.created",
                actor_id=ACTOR,
                actor_type="service",
                resource_id=tenant,
            )
        return True
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--name", required=True)
    arguments = parser.parse_args()
    try:
        added = asyncio.run(bootstrap(arguments.tenant, arguments.name.strip()))
    except BrandingError as error:
        print(error, file=sys.stderr)
        return 2
    print("Recorded" if added else "Already recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
