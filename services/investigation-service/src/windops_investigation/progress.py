"""Each case's progress, in order, for the live stream (API-003, ADR 0025). Events never change."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from windops_investigation.store import PROGRESS


async def add_progress(
    connection: AsyncConnection, case: Any, *, key: str, kind: str, at: datetime, **values: Any
) -> bool:
    """Add one event, after the case's last, unless one with this key is there. The caller holds
    the case's row lock, so two replicas never take the same sequence."""
    p = PROGRESS.c
    seen = await connection.execute(
        select(p.sequence).where(p.tenant_id == case.tenant_id, p.case_id == case.case_id,
                                 p.event_key == key)
    )  # fmt: skip
    if seen.first() is not None:
        return False
    last = (await connection.execute(
        select(func.max(p.sequence)).where(p.tenant_id == case.tenant_id, p.case_id == case.case_id)
    )).scalar()  # fmt: skip
    await connection.execute(PROGRESS.insert().values(
        tenant_id=case.tenant_id, case_id=case.case_id, sequence=(last or 0) + 1, at=at,
        kind=kind, event_key=key, **values,
    ))  # fmt: skip
    return True
