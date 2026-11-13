"""A tenant's rules, as the API and the detector both need them."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_migrations import in_tenant

from windops_fault_detection import rules
from windops_fault_detection.defaults import DEFAULT_RULES, LIBRARY
from windops_fault_detection.store import FAULT_RULE_VERSIONS, FAULT_RULES, audit

SYSTEM = "system"


async def write_version(
    connection: AsyncConnection, tenant: str, rule_id: str, version: int,
    definition: dict[str, Any], by: str, reason: str, now: datetime,
) -> None:  # fmt: skip
    await connection.execute(
        FAULT_RULE_VERSIONS.insert().values(
            tenant_id=tenant, rule_id=rule_id, version=version, kind=definition["kind"],
            definition=rules.canonical(definition),
            content_hash=rules.content_hash(rule_id, version, definition), created_by=by,
            created_at=now, reason=reason,
        )
    )  # fmt: skip


async def install_defaults(engine: AsyncEngine, tenant: str, now: datetime) -> None:
    """A tenant's first look installs the default library, as version 1 of each, audited."""
    async with engine.connect() as connection:
        await in_tenant(connection, tenant)
        any_rule = await connection.execute(
            select(FAULT_RULES.c.rule_id).where(FAULT_RULES.c.tenant_id == tenant).limit(1)
        )
        if any_rule.first() is not None:
            return
    try:
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            for rule_id, name, description, definition in DEFAULT_RULES:
                await write_version(connection, tenant, rule_id, 1, definition, SYSTEM,
                                    f"The default library, {LIBRARY}", now)  # fmt: skip
                await connection.execute(
                    FAULT_RULES.insert().values(
                        tenant_id=tenant, rule_id=rule_id, name=name, description=description,
                        enabled=True, current_version=1, created_at=now, updated_at=now,
                    )
                )  # fmt: skip
                await audit(connection, tenant_id=tenant, actor_type=SYSTEM, actor_id=SYSTEM,
                            action="fault_rule.created", rule_id=rule_id)  # fmt: skip
    except IntegrityError:
        # Another call installed them first.
        return


async def enabled_rules(connection: AsyncConnection, tenant: str) -> list[tuple[rules.Rule, str]]:
    """Each enabled rule at its current version, with that version's content hash."""
    r, v = FAULT_RULES.c, FAULT_RULE_VERSIONS.c
    rows = (
        await connection.execute(
            select(r.rule_id, r.name, v.version, v.definition, v.content_hash)
            .join(
                FAULT_RULE_VERSIONS,
                (v.tenant_id == r.tenant_id)
                & (v.rule_id == r.rule_id)
                & (v.version == r.current_version),
            )
            .where(r.tenant_id == tenant, r.enabled.is_(True))
            .order_by(r.rule_id)
        )
    ).all()
    return [
        (
            rules.parse(row.rule_id, row.version, row.name, json.loads(row.definition)),
            row.content_hash,
        )
        for row in rows
    ]
