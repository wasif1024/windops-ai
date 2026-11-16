"""A tenant's reference data, as the API and each estimate need it."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_migrations import in_tenant

from windops_production_impact import references
from windops_production_impact.defaults import DEFAULT_REFERENCES, LIBRARY
from windops_production_impact.store import REFERENCE_VERSIONS, REFERENCES, audit

SYSTEM = "system"


@dataclass(frozen=True)
class Current:
    reference_id: str
    kind: str
    version: int
    content_hash: str
    parsed: Any


async def write_version(
    connection: AsyncConnection, tenant: str, reference_id: str, version: int,
    definition: dict[str, Any], by: str, reason: str, now: datetime,
) -> None:  # fmt: skip
    await connection.execute(
        REFERENCE_VERSIONS.insert().values(
            tenant_id=tenant, reference_id=reference_id, version=version,
            kind=definition["kind"], definition=references.canonical(definition),
            content_hash=references.content_hash(reference_id, version, definition),
            created_by=by, created_at=now, reason=reason,
        )
    )  # fmt: skip


async def install_defaults(engine: AsyncEngine, tenant: str, now: datetime) -> None:
    """A tenant's first look installs the defaults, as version 1 of each, audited.

    A tenant that has its references gets any default it lacks, added by a later library, the
    same way. One it has is never touched: it may be an administrator's.
    """
    async with engine.connect() as connection:
        await in_tenant(connection, tenant)
        found = await connection.execute(
            select(REFERENCES.c.reference_id).where(REFERENCES.c.tenant_id == tenant)
        )
        have = set(found.scalars())
    missing = [d for d in DEFAULT_REFERENCES if d[0] not in have]
    if not missing:
        return
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            for reference_id, name, definition in missing:
                await write_version(connection, tenant, reference_id, 1, definition, SYSTEM,
                                    f"The defaults, {LIBRARY}", now)  # fmt: skip
                await connection.execute(
                    REFERENCES.insert().values(
                        tenant_id=tenant, reference_id=reference_id, name=name,
                        kind=definition["kind"], current_version=1, created_at=now,
                        updated_at=now,
                    )
                )  # fmt: skip
                await audit(
                    connection, tenant_id=tenant, actor_type=SYSTEM, actor_id=SYSTEM,
                    action="impact_reference.created", resource_id=reference_id,
                )  # fmt: skip
    except IntegrityError:
        # Another call installed them first.
        return


async def current(connection: AsyncConnection, tenant: str) -> dict[str, Current]:
    """Each reference at its current version, parsed, by its ID."""
    r, v = REFERENCES.c, REFERENCE_VERSIONS.c
    rows = (
        await connection.execute(
            select(r.reference_id, v.kind, v.version, v.definition, v.content_hash)
            .join(
                REFERENCE_VERSIONS,
                (v.tenant_id == r.tenant_id)
                & (v.reference_id == r.reference_id)
                & (v.version == r.current_version),
            )
            .where(r.tenant_id == tenant)
        )
    ).all()
    return {
        row.reference_id: Current(
            row.reference_id,
            row.kind,
            row.version,
            row.content_hash,
            references.parse(json.loads(row.definition)),
        )
        for row in rows
    }
