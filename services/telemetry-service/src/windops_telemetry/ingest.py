"""The device route: batches of samples over HTTPS, each device with its own credential (API-004).

The gateway sends /ingest/* here with no user sign-in. Each request carries one device's
credential, for one tenant and turbine (FR-TEL-005). A sample for any other tenant or turbine is
refused, as is one that breaks the schema, and each refusal is recorded with its reason rather
than dropped (FR-TEL-003). The accepted samples and the refusals are stored together.
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from windops_migrations import in_tenant

from windops_telemetry import credentials
from windops_telemetry.limits import LimitUnavailableError
from windops_telemetry.schema import MalformedError, parse
from windops_telemetry.store import (
    DEVICE_CREDENTIALS,
    EXCERPT_CHARACTERS,
    INGEST_REJECTIONS,
    OPERATIONAL_SAMPLES,
)

PREFIX = "/ingest/v1"
MAXIMUM_BODY_BYTES = 1024 * 1024
MAXIMUM_SAMPLES = 600
# A sample timed further ahead than this has a broken clock. It is refused, with its reason.
FUTURE_TOLERANCE = timedelta(hours=1)


class Rejected(BaseModel):
    index: int
    # A fixed code, such as "bad_event_time" or "wrong_turbine".
    reason: str


class Receipt(BaseModel):
    accepted: int
    rejected: list[Rejected]


@dataclass(frozen=True)
class Device:
    tenant_id: str
    device_id: str
    turbine_id: str


def problem(status: int, title: str, code: str, **headers: str) -> JSONResponse:
    """An RFC 9457 problem in our own words, which never repeats what was sent."""
    body = {"type": "about:blank", "title": title, "status": status, "code": code}
    return JSONResponse(
        body, status_code=status, headers=headers, media_type="application/problem+json"
    )


class RefusedError(Exception):
    def __init__(self, response: JSONResponse) -> None:
        self.response = response


async def _bounded(request: Request) -> bytes | None:
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > MAXIMUM_BODY_BYTES:
            return None
    return bytes(received)


def _excerpt(item: object) -> str:
    text = item if isinstance(item, str) else json.dumps(item, default=str)
    return text[:EXCERPT_CHARACTERS]


def create_ingest_router(
    engine: AsyncEngine, limiter: Any, clock: Callable[[], datetime] | None = None
) -> APIRouter:
    router = APIRouter(prefix=PREFIX)
    now = clock or (lambda: datetime.now(UTC))

    async def device_for(header: str | None) -> Device:
        """The device a credential belongs to. Every way of failing gets the same answer."""
        refused = RefusedError(
            problem(
                401,
                "A valid device credential is required",
                "bad_credential",
                **{"WWW-Authenticate": "Bearer"},
            )
        )
        shown = credentials.presented(header)
        if shown is None:
            raise refused
        async with engine.begin() as connection:
            await in_tenant(connection, shown.tenant_id)
            row = (
                await connection.execute(
                    select(DEVICE_CREDENTIALS).where(
                        DEVICE_CREDENTIALS.c.tenant_id == shown.tenant_id,
                        DEVICE_CREDENTIALS.c.device_id == shown.device_id,
                        DEVICE_CREDENTIALS.c.revoked_at.is_(None),
                    )
                )
            ).one_or_none()
        if row is None or not credentials.matches(shown.secret, row.secret_hash):
            raise refused
        return Device(row.tenant_id, row.device_id, row.turbine_id)

    @router.post("/samples", response_model=Receipt, summary="A device sends a batch of samples")
    async def samples(request: Request) -> Any:
        try:
            device = await device_for(request.headers.get("authorization"))
            try:
                decision = await limiter.check(device.tenant_id, device.device_id)
            except LimitUnavailableError as error:
                raise RefusedError(
                    problem(503, "Temporarily unavailable", "unavailable")
                ) from error
            if not decision.allowed:
                raise RefusedError(
                    problem(
                        429, "Too many requests", "too_many_requests",
                        **{"Retry-After": str(decision.reset_seconds)},
                    )
                )  # fmt: skip
            body = await _bounded(request)
            if body is None:
                raise RefusedError(problem(413, "A batch is at most 1 MiB", "too_large"))
            try:
                document = json.loads(body)
            except (ValueError, UnicodeDecodeError) as error:
                raise RefusedError(problem(400, "The body is not JSON", "not_json")) from error
            items = document.get("samples") if isinstance(document, dict) else None
            if not isinstance(items, list):
                raise RefusedError(problem(400, 'Send {"samples": [...]}', "no_samples"))
            if len(items) > MAXIMUM_SAMPLES:
                raise RefusedError(
                    problem(
                        413, f"A batch is at most {MAXIMUM_SAMPLES} samples", "too_many_samples"
                    )
                )
        except RefusedError as refusal:
            return refusal.response
        return await store(device, items)

    async def store(device: Device, items: list[Any]) -> Receipt:
        received = now()
        rows: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        receipts: list[Rejected] = []
        for index, item in enumerate(items):
            # A device that loses its connection part way may send a broken line as text.
            reason = "not_json" if isinstance(item, str) else ""
            if not reason:
                try:
                    sample = parse(item)
                    if sample.tenant_id != device.tenant_id:
                        reason = "wrong_tenant"
                    elif sample.turbine_id != device.turbine_id:
                        reason = "wrong_turbine"
                    elif sample.event_time > received + FUTURE_TOLERANCE:
                        reason = "event_time_in_future"
                except MalformedError as error:
                    reason = error.reason
            if reason:
                receipts.append(Rejected(index=index, reason=reason))
                rejections.append(
                    {
                        "tenant_id": device.tenant_id,
                        "rejection_id": uuid.uuid4().hex,
                        "device_id": device.device_id,
                        "turbine_id": device.turbine_id,
                        "received_at": received,
                        "reason": reason,
                        "excerpt": _excerpt(item),
                    }
                )
                continue
            rows.append(
                {
                    "tenant_id": device.tenant_id,
                    "turbine_id": device.turbine_id,
                    "event_time": sample.event_time,
                    "sequence": sample.sequence,
                    "device_id": device.device_id,
                    "ingested_at": received,
                    "source": sample.source,
                    "schema_version": sample.schema_version,
                    "model_version": sample.model_version,
                    "synthetic": sample.synthetic,
                    "operating_state": sample.operating_state,
                    "available": sample.available,
                    "quality_status": "received",
                    **sample.readings,
                }
            )
        async with engine.begin() as connection:
            await in_tenant(connection, device.tenant_id)
            if rows:
                await connection.execute(OPERATIONAL_SAMPLES.insert(), rows)
            if rejections:
                await connection.execute(INGEST_REJECTIONS.insert(), rejections)
        return Receipt(accepted=len(rows), rejected=receipts)

    return router
