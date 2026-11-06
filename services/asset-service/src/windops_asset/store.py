"""The tables this service owns, all tenant-owned with row-level security, and its events."""

import hashlib
from datetime import datetime
from typing import Any

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    LargeBinary,
    MetaData,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_outbox import add_event

SERVICE_NAME = "asset"
EVIDENCE_SUBJECT = "evidence.changed"
METADATA = MetaData()

WIND_FARMS = Table(
    "wind_farms",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("wind_farm_id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("latitude", Float, nullable=False),
    Column("longitude", Float, nullable=False),
    Column("time_zone", Text, nullable=False),
    Column("offshore", Boolean, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
TURBINES = Table(
    "turbines",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("turbine_id", Text, primary_key=True),
    Column("wind_farm_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("latitude", Float, nullable=False),
    Column("longitude", Float, nullable=False),
    Column("manufacturer", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("rated_power_kw", Float, nullable=False),
    Column("rotor_diameter_m", Float, nullable=False),
    Column("hub_height_m", Float, nullable=False),
    Column("serial_number", Text, nullable=False),
    Column("commissioned_on", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("status_reason", Text, nullable=False),
    Column("status_changed_at", DateTime(timezone=True), nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
COMPONENTS = Table(
    "components",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("component_id", Text, primary_key=True),
    Column("turbine_id", Text, nullable=False),
    Column("parent_component_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("manufacturer", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("serial_number", Text, nullable=False),
    Column("installed_on", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("status_reason", Text, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
# A retry with the same key returns the first answer, and changes nothing (GRP-007).
IDEMPOTENCY_KEYS = Table(
    "idempotency_keys",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("user_id", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("method", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("response", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = ("wind_farms", "turbines", "components", "idempotency_keys")


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def _hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def remembered(
    connection: AsyncConnection,
    tenant_id: str,
    user_id: str,
    key: str,
    method: str,
    request: Message,
) -> bytes | None:
    """Return the first answer to a retried call, or None if this key is new."""
    if not key:
        return None
    row = (
        await connection.execute(
            select(IDEMPOTENCY_KEYS).where(
                IDEMPOTENCY_KEYS.c.tenant_id == tenant_id,
                IDEMPOTENCY_KEYS.c.user_id == user_id,
                IDEMPOTENCY_KEYS.c.idempotency_key == key,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    if row.method != method or row.request_hash != _hash(request):
        raise KeyReusedError("This idempotency key was used for a different request")
    response: bytes = row.response
    return response


async def remember(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    user_id: str,
    key: str,
    method: str,
    request: Message,
    response: Message,
    now: datetime,
) -> None:
    """Keep the answer in the change's own transaction, so a retry finds one or neither."""
    if key:
        await connection.execute(
            insert(IDEMPOTENCY_KEYS).values(
                tenant_id=tenant_id,
                user_id=user_id,
                idempotency_key=key,
                method=method,
                request_hash=_hash(request),
                response=response.SerializeToString(),
                created_at=now,
            )
        )


async def record_change(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    turbine: Any | None = None,
    actor_type: str = "user",
) -> None:
    """Queue the audit record, and for a turbine its evidence.changed, with the change (EVT-002)."""
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )
    if turbine is not None:
        await add_event(
            connection,
            EVIDENCE_SUBJECT,
            tenant_id=tenant_id,
            payload=events_pb2.EvidenceChanged(
                source_service=SERVICE_NAME,
                record_id=turbine.turbine_id,
                version=str(turbine.version),
            ),
            producer=SERVICE_NAME,
        )
