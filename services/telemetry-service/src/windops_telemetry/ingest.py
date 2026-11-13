"""The device routes: samples and CMS batches over HTTPS, each device with its own credential.

API-004. `/samples` takes batches of 1 Hz operational samples, and `/cms` one CMS vibration
batch, whose windows become spectral features (FR-TEL-004), and are kept raw on a schedule or a
device's trigger.

The gateway sends /ingest/* here with no user sign-in. Each request carries one device's
credential, for one tenant and turbine (FR-TEL-005). A sample for any other tenant or turbine is
refused, as is one that breaks the schema, and each refusal is recorded with its reason rather
than dropped (FR-TEL-003). The accepted samples and the refusals are stored together.
"""

import asyncio
import json
import struct
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Table, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_migrations import in_tenant

from windops_telemetry import credentials, spectra
from windops_telemetry.freshness import IngestMetrics
from windops_telemetry.limits import LimitUnavailableError
from windops_telemetry.schema import (
    MalformedError,
    ParsedAlarm,
    parse,
    parse_alarm,
    parse_cms,
)
from windops_telemetry.store import (
    CMS_FEATURES,
    CMS_WAVEFORMS,
    DEVICE_CREDENTIALS,
    EXCERPT_CHARACTERS,
    INGEST_REJECTIONS,
    OPERATIONAL_SAMPLES,
    queue_alarm,
)

PREFIX = "/ingest/v1"
MAXIMUM_BODY_BYTES = 1024 * 1024
MAXIMUM_SAMPLES = 600
# A controller sends a few alarms at a time. This is far more than a real one ever needs.
MAXIMUM_ALARMS = 200
# A sample timed further ahead than this has a broken clock. It is refused, with its reason.
FUTURE_TOLERANCE = timedelta(hours=1)
# A CMS window comes every 10 minutes. The first in each scheduled block keeps its raw values.
CMS_PERIOD_SECONDS = 600


class Rejected(BaseModel):
    index: int
    # A fixed code, such as "bad_event_time" or "wrong_turbine".
    reason: str


class Receipt(BaseModel):
    accepted: int
    rejected: list[Rejected]
    # Windows already stored, sent again. They are kept once.
    duplicates: int = 0


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


def _without_values(document: object) -> object:
    """A CMS batch, without its long windows, so its excerpt shows what was wrong."""
    if not isinstance(document, dict) or not isinstance(document.get("sensors"), list):
        return document
    sensors = [
        {k: v for k, v in item.items() if k != "values"} if isinstance(item, dict) else item
        for item in document["sensors"]
    ]
    return {**document, "sensors": sensors}


def _insert_ignoring(connection: AsyncConnection, table: Table) -> Any:
    dialect = postgresql if connection.dialect.name == "postgresql" else sqlite
    return dialect.insert(table).on_conflict_do_nothing()


def _readable(item: object) -> dict[str, Any]:
    """The sequence and time of a refused sample, where they can still be read."""
    if not isinstance(item, dict):
        return {"sequence": None, "event_time": None}
    sequence = item.get("sequence")
    moment = None
    if isinstance(item.get("event_time"), str):
        try:
            moment = datetime.fromisoformat(item["event_time"])
        except ValueError:
            moment = None
    return {
        "sequence": sequence if isinstance(sequence, int) and not isinstance(sequence, bool)
        and 0 < sequence < 2**62 else None,
        "event_time": moment if moment is not None and moment.tzinfo else None,
    }  # fmt: skip


def _excerpt(item: object) -> str:
    text = item if isinstance(item, str) else json.dumps(item, default=str)
    return text[:EXCERPT_CHARACTERS]


def create_ingest_router(
    engine: AsyncEngine,
    limiter: Any,
    clock: Callable[[], datetime] | None = None,
    raw_every_hours: int = 6,
    measures: IngestMetrics | None = None,
) -> APIRouter:
    router = APIRouter(prefix=PREFIX)
    now = clock or (lambda: datetime.now(UTC))
    # Delay and outcome of each sample, for the Data Freshness dashboard (M4.9).
    measured = measures or IngestMetrics()

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

    async def receive(request: Request) -> tuple[Device, Any]:
        """The device, and its body as JSON, once its credential, limit and size are checked."""
        device = await device_for(request.headers.get("authorization"))
        try:
            decision = await limiter.check(device.tenant_id, device.device_id)
        except LimitUnavailableError as error:
            raise RefusedError(problem(503, "Temporarily unavailable", "unavailable")) from error
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
            return device, json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise RefusedError(problem(400, "The body is not JSON", "not_json")) from error

    @router.post("/samples", response_model=Receipt, summary="A device sends a batch of samples")
    async def samples(request: Request) -> Any:
        try:
            device, document = await receive(request)
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

    @router.post(
        "/alarms", response_model=Receipt, summary="A turbine's controller raises or clears alarms"
    )
    async def alarms(request: Request) -> Any:
        try:
            device, document = await receive(request)
            items = document.get("alarms") if isinstance(document, dict) else None
            if not isinstance(items, list):
                raise RefusedError(problem(400, 'Send {"alarms": [...]}', "no_alarms"))
            if len(items) > MAXIMUM_ALARMS:
                raise RefusedError(
                    problem(413, f"A batch is at most {MAXIMUM_ALARMS} alarms", "too_many_alarms")
                )
        except RefusedError as refusal:
            return refusal.response
        return await store_alarms(device, items)

    async def store_alarms(device: Device, items: list[Any]) -> Receipt:
        """Queue each alarm as alarm.reported, for the Alarm Service (ADR 0007). A broken one is
        kept with its reason, as a broken sample is (FR-TEL-003)."""
        received = now()
        reports: list[ParsedAlarm] = []
        rejections: list[dict[str, Any]] = []
        receipts: list[Rejected] = []
        for index, item in enumerate(items):
            reason = ""
            try:
                report = parse_alarm(item)
                if report.tenant_id != device.tenant_id:
                    reason = "wrong_tenant"
                elif report.turbine_id != device.turbine_id:
                    reason = "wrong_turbine"
                elif report.event_time > received + FUTURE_TOLERANCE:
                    reason = "event_time_in_future"
            except MalformedError as error:
                reason = error.reason
            if reason:
                receipts.append(Rejected(index=index, reason=f"alarm_{reason}"))
                rejections.append(
                    {
                        "tenant_id": device.tenant_id, "rejection_id": uuid.uuid4().hex,
                        "device_id": device.device_id, "turbine_id": device.turbine_id,
                        "received_at": received, "reason": f"alarm_{reason}",
                        "excerpt": _excerpt(item),
                    }
                )  # fmt: skip
                continue
            reports.append(report)
        queued: list[ParsedAlarm] = []
        async with engine.begin() as connection:
            await in_tenant(connection, device.tenant_id)
            for report in reports:
                if await queue_alarm(connection, device, report, received):
                    queued.append(report)
            if rejections:
                await connection.execute(INGEST_REJECTIONS.insert(), rejections)
        measured.accepted(device.tenant_id, "alarms", received, [r.event_time for r in queued])
        measured.rejected(device.tenant_id, "alarms", [r.reason for r in receipts])
        # A report sent again is accepted, and kept once.
        return Receipt(
            accepted=len(queued), rejected=receipts, duplicates=len(reports) - len(queued)
        )

    @router.post("/cms", response_model=Receipt, summary="A device sends one CMS vibration batch")
    async def cms(request: Request) -> Any:
        try:
            device, document = await receive(request)
        except RefusedError as refusal:
            return refusal.response
        return await store_cms(device, document)

    async def store_cms(device: Device, document: Any) -> Receipt:
        received = now()
        reason = ""
        try:
            batch = parse_cms(document)
            if batch.tenant_id != device.tenant_id:
                reason = "wrong_tenant"
            elif batch.turbine_id != device.turbine_id:
                reason = "wrong_turbine"
            elif batch.event_time > received + FUTURE_TOLERANCE:
                reason = "event_time_in_future"
        except MalformedError as error:
            reason = error.reason
        if reason:
            async with engine.begin() as connection:
                await in_tenant(connection, device.tenant_id)
                await connection.execute(
                    INGEST_REJECTIONS.insert().values(
                        tenant_id=device.tenant_id, rejection_id=uuid.uuid4().hex,
                        device_id=device.device_id, turbine_id=device.turbine_id,
                        received_at=received, reason=f"cms_{reason}",
                        excerpt=_excerpt(_without_values(document)),
                    )
                )  # fmt: skip
            measured.rejected(device.tenant_id, "cms", [f"cms_{reason}"])
            return Receipt(accepted=0, rejected=[Rejected(index=0, reason=f"cms_{reason}")])
        # The transforms take a few tens of milliseconds each, so they run off the event loop.
        computed = await asyncio.to_thread(
            lambda: [
                spectra.features(
                    window.values, batch.sample_rate_hz, window.sensor,
                    batch.rotor_speed_rpm, batch.generator_speed_rpm,
                )
                for window in batch.windows
            ]
        )  # fmt: skip
        kept_for = (
            f"trigger:{batch.trigger}"
            if batch.trigger
            else "schedule"
            if int(batch.event_time.timestamp()) % (raw_every_hours * 3600) < CMS_PERIOD_SECONDS
            else None
        )
        stored = duplicates = 0
        async with engine.begin() as connection:
            await in_tenant(connection, device.tenant_id)
            for window, values in zip(batch.windows, computed, strict=True):
                window_id = uuid.uuid4().hex if kept_for else None
                key = {
                    "tenant_id": device.tenant_id, "turbine_id": device.turbine_id,
                    "sensor": window.sensor, "event_time": batch.event_time,
                }  # fmt: skip
                inserted = await connection.execute(
                    _insert_ignoring(connection, CMS_FEATURES).values(
                        **key, sequence=batch.sequence, device_id=device.device_id,
                        ingested_at=received, source=batch.source,
                        schema_version=batch.schema_version, model_version=batch.model_version,
                        synthetic=batch.synthetic, feature_version=spectra.FEATURE_VERSION,
                        sample_rate_hz=batch.sample_rate_hz, samples=len(window.values),
                        rotor_speed_rpm=batch.rotor_speed_rpm,
                        generator_speed_rpm=batch.generator_speed_rpm,
                        shaft_hz=spectra.shaft_hz(window.sensor, batch.rotor_speed_rpm,
                                                  batch.generator_speed_rpm),
                        quality_status="received", window_id=window_id, trigger=batch.trigger,
                        **values,
                    )
                )  # fmt: skip
                if not inserted.rowcount:
                    # The same window sent again: kept once.
                    duplicates += 1
                    continue
                stored += 1
                if kept_for and window_id:
                    await connection.execute(
                        _insert_ignoring(connection, CMS_WAVEFORMS).values(
                            **key, window_id=window_id, ingested_at=received,
                            sample_rate_hz=batch.sample_rate_hz, samples=len(window.values),
                            unit=window.unit, kept_for=kept_for,
                            values_f32le=struct.pack(f"<{len(window.values)}f", *window.values),
                        )
                    )  # fmt: skip
        measured.accepted(device.tenant_id, "cms", received, [batch.event_time] * stored)
        return Receipt(accepted=stored, rejected=[], duplicates=duplicates)

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
                        **_readable(item),
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
                    "arrival_index": index,
                    **sample.readings,
                }
            )
        async with engine.begin() as connection:
            await in_tenant(connection, device.tenant_id)
            if rows:
                await connection.execute(OPERATIONAL_SAMPLES.insert(), rows)
            if rejections:
                await connection.execute(INGEST_REJECTIONS.insert(), rejections)
        measured.accepted(device.tenant_id, "samples", received, [r["event_time"] for r in rows])
        measured.rejected(device.tenant_id, "samples", [r.reason for r in receipts])
        return Receipt(accepted=len(rows), rejected=receipts)

    return router
