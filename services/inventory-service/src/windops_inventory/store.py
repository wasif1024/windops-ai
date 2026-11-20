"""The tables this service owns, all tenant-owned with row-level security, and its events."""

import hashlib
from datetime import datetime
from typing import Any

from google.protobuf.message import Message
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
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

SERVICE_NAME = "inventory"
EVIDENCE_SUBJECT = "evidence.changed"
HELD, RELEASED = "held", "released"
METADATA = MetaData()

# The catalog: one row a part.
PARTS = Table(
    "parts",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("part_number", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("component_kind", Text, nullable=False),
    Column("unit", Text, nullable=False),
    Column("fits_any_model", Boolean, nullable=False),
    Column("retired", Boolean, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("updated_by", Text, nullable=False),
)
# The turbine models each part fits, as given and as matched: trimmed and lowercased.
PART_MODELS = Table(
    "part_models",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("part_number", Text, primary_key=True),
    Column("manufacturer_key", Text, primary_key=True),
    Column("model_key", Text, primary_key=True),
    Column("manufacturer", Text, nullable=False),
    Column("model", Text, nullable=False),
)
# One row a part in a wind farm's store.
STOCK = Table(
    "stock",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("part_number", Text, primary_key=True),
    Column("wind_farm_id", Text, primary_key=True),
    Column("on_hand", BigInteger, nullable=False),
    Column("reserved", BigInteger, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # What is held can never be more than what is there.
    CheckConstraint("reserved >= 0 AND on_hand >= reserved", name="stock_covers_reserved"),
)
RESERVATIONS = Table(
    "reservations",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("reservation_id", Text, primary_key=True),
    Column("work_order_id", Text, nullable=False),
    Column("part_number", Text, nullable=False),
    Column("wind_farm_id", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("reserved_by", Text, nullable=False),
    Column("reserved_at", DateTime(timezone=True), nullable=False),
    Column("released_by", Text, nullable=False, server_default=""),
    Column("released_at", DateTime(timezone=True)),
    Column("release_reason", Text, nullable=False, server_default=""),
)
# Every change to a store's stock, as it happened: never changed, never deleted.
STOCK_MOVEMENTS = Table(
    "stock_movements",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("part_number", Text, primary_key=True),
    Column("wind_farm_id", Text, primary_key=True),
    # The stock row's version after it, so from 1, one a change.
    Column("position", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True), nullable=False),
    # "adjusted", "reserved" or "released".
    Column("kind", Text, nullable=False),
    # What it did to on_hand or reserved.
    Column("quantity", BigInteger, nullable=False),
    Column("on_hand", BigInteger, nullable=False),
    Column("reserved", BigInteger, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("reservation_id", Text, nullable=False, server_default=""),
    Column("reason", Text, nullable=False, server_default=""),
)
# A retry with the same key returns the first answer, and changes nothing (GRP-007).
IDEMPOTENCY_KEYS = Table(
    "idempotency_keys",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("caller", Text, primary_key=True),
    Column("idempotency_key", Text, primary_key=True),
    Column("method", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Column("response", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
TENANT_TABLES = (
    "parts",
    "part_models",
    "stock",
    "reservations",
    "stock_movements",
    "idempotency_keys",
)


class KeyReusedError(ValueError):
    """The same key came with a different request, so it cannot be a retry."""


def _hash(request: Message) -> str:
    return hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()


async def denied(connection: AsyncConnection, identity: Any, permission: str) -> None:
    """Record a refusal made here, before Authorization was asked, as Authorization records its
    own, so the Denials page shows it too (SRS 9.4). An agent is the actor, on its principal's
    behalf (SEC-010)."""
    actor = identity.user_id or identity.subject
    await add_event(
        connection, audit_request_subject(SERVICE_NAME), tenant_id=identity.tenant_id,
        payload=events_pb2.AuditRequested(
            action="authorization.denied", actor_type="agent" if identity.actor else (
                "user" if identity.user_id else "service"),
            actor_id=identity.actor or actor, on_behalf_of=actor if identity.actor else "",
            resource_type="permission", resource_id=permission, outcome="denied",
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip


async def remembered(
    connection: AsyncConnection, tenant_id: str, caller: str, key: str, method: str,
    request: Message,
) -> bytes | None:  # fmt: skip
    """The first answer to a retried call, or None if this key is new."""
    if not key:
        return None
    k = IDEMPOTENCY_KEYS.c
    row = (await connection.execute(
        select(IDEMPOTENCY_KEYS).where(k.tenant_id == tenant_id, k.caller == caller,
                                       k.idempotency_key == key)
    )).one_or_none()  # fmt: skip
    if row is None:
        return None
    if row.method != method or row.request_hash != _hash(request):
        raise KeyReusedError("This idempotency key was used for a different request")
    response: bytes = row.response
    return response


async def remember(
    connection: AsyncConnection, *, tenant_id: str, caller: str, key: str, method: str,
    request: Message, response: Message, now: datetime,
) -> None:  # fmt: skip
    if key:
        await connection.execute(insert(IDEMPOTENCY_KEYS).values(
            tenant_id=tenant_id, caller=caller, idempotency_key=key, method=method,
            request_hash=_hash(request), response=response.SerializeToString(), created_at=now,
        ))  # fmt: skip


async def audit(
    connection: AsyncConnection, tenant_id: str, *, action: str, actor_type: str, actor_id: str,
    resource_type: str, resource_id: str,
) -> None:  # fmt: skip
    """Audited in the same transaction as the change (EVT-002). Only a person changes anything
    here, so the actor is always the one who did it."""
    await add_event(
        connection, audit_request_subject(SERVICE_NAME), tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=f"inventory.{action}", actor_type=actor_type, actor_id=actor_id,
            resource_type=resource_type, resource_id=resource_id, outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )  # fmt: skip


async def announce(connection: AsyncConnection, tenant_id: str, record_id: str,
                   version: int) -> None:  # fmt: skip
    """A part or a store's stock changed, which may make a recommendation stale (SRS 8.5)."""
    await add_event(
        connection, EVIDENCE_SUBJECT, tenant_id=tenant_id,
        payload=events_pb2.EvidenceChanged(source_service=SERVICE_NAME, record_id=record_id,
                                           version=str(version)),
        producer=SERVICE_NAME,
    )  # fmt: skip


async def move(
    connection: AsyncConnection, stock: Any, *, at: datetime, kind: str, quantity: int,
    actor_id: str, reservation_id: str = "", reason: str = "",
) -> None:  # fmt: skip
    """Keep a change to a store's stock in its ledger, as the stock row now stands."""
    await connection.execute(insert(STOCK_MOVEMENTS).values(
        tenant_id=stock.tenant_id, part_number=stock.part_number,
        wind_farm_id=stock.wind_farm_id, position=stock.version, at=at, kind=kind,
        quantity=quantity, on_hand=stock.on_hand, reserved=stock.reserved, actor_id=actor_id,
        reservation_id=reservation_id, reason=reason,
    ))  # fmt: skip


def part_record(part_number: str) -> str:
    return f"part:{part_number}"


def stock_record(part_number: str, wind_farm_id: str) -> str:
    return f"stock:{part_number}@{wind_farm_id}"
