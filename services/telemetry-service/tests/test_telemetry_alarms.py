"""The device route's alarms: checked, queued as alarm.reported, and never lost (ADR 0007)."""

import asyncio
from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from test_telemetry_ingest import RECEIVED, TENANT, TURBINE, FakeRedis, add_credential
from windops.events.v1 import events_pb2
from windops_events import parse_event
from windops_outbox import OUTBOX_TABLE, create_outbox_table
from windops_telemetry.ingest import MAXIMUM_ALARMS, PREFIX, create_ingest_router
from windops_telemetry.limits import DeviceLimiter
from windops_telemetry.store import INGEST_REJECTIONS, METADATA

URL = f"{PREFIX}/alarms"


def report(**fields: Any) -> dict[str, Any]:
    return {
        "schema_version": "1", "tenant_id": TENANT, "turbine_id": TURBINE,
        "report_id": "GBX_OIL_TEMP_HIGH:gearbox_oil_temp_c:raise:1768456800",
        "code": "GBX_OIL_TEMP_HIGH", "tag": "gearbox_oil_temp_c", "action": "raise",
        "event_time": (RECEIVED - timedelta(seconds=3)).isoformat(), "severity": "high",
        "message": "Gearbox oil temperature high", "source": "windops-simulator",
        **fields,
    }  # fmt: skip


async def queued(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE))).all()
    return [parse_event(row.subject, row.envelope) for row in rows]


def with_route(scenario: Any) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        token = await add_credential(engine)
        limiter = DeviceLimiter(FakeRedis(), clock=lambda: 1_800_000_030.0)
        app = FastAPI()
        app.include_router(create_ingest_router(engine, limiter, clock=lambda: RECEIVED))
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="https://ingest") as client:

                async def post(body: Any) -> httpx.Response:
                    return await client.post(
                        URL, json=body, headers={"Authorization": f"Bearer {token}"}
                    )

                await scenario(engine, post)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_an_alarm_is_queued_as_alarm_reported_with_everything_the_device_said() -> None:
    async def scenario(engine: AsyncEngine, post: Any) -> None:
        answer = await post({"alarms": [report()]})
        assert answer.status_code == 200
        assert answer.json() == {"accepted": 1, "rejected": [], "duplicates": 0}
        (event,) = await queued(engine)
        assert (event.subject, event.tenant_id, event.producer) == (
            "alarm.reported", TENANT, "telemetry",
        )  # fmt: skip
        payload = event.payload
        assert isinstance(payload, events_pb2.AlarmReported)
        assert (payload.turbine_id, payload.code, payload.tag, payload.action) == (
            TURBINE, "GBX_OIL_TEMP_HIGH", "gearbox_oil_temp_c", "raise",
        )  # fmt: skip
        assert (payload.severity, payload.source, payload.device_id != "") == (
            "high", "windops-simulator", True,
        )  # fmt: skip
        assert payload.received_at.ToDatetime().replace(tzinfo=RECEIVED.tzinfo) == RECEIVED

    with_route(scenario)


def test_a_report_sent_again_is_queued_once() -> None:
    async def scenario(engine: AsyncEngine, post: Any) -> None:
        await post({"alarms": [report()]})
        again = await post({"alarms": [report(), report(report_id="other:1")]})
        assert again.json() == {"accepted": 1, "rejected": [], "duplicates": 1}
        assert len(await queued(engine)) == 2

    with_route(scenario)


def test_a_broken_or_foreign_report_is_refused_with_its_reason_and_kept() -> None:
    """FR-TEL-003, as for samples: nothing is dropped silently."""

    async def scenario(engine: AsyncEngine, post: Any) -> None:
        answer = await post(
            {
                "alarms": [
                    report(code="lowercase"),
                    report(action="flicker"),
                    report(severity="urgent"),
                    report(turbine_id="hornsea-west-t01"),
                    report(event_time=(RECEIVED + timedelta(hours=2)).isoformat()),
                    "not even an object",
                ]
            }
        )
        reasons = [r["reason"] for r in answer.json()["rejected"]]
        assert reasons == [
            "alarm_bad_code", "alarm_bad_action", "alarm_bad_severity", "alarm_wrong_turbine",
            "alarm_event_time_in_future", "alarm_not_an_object",
        ]  # fmt: skip
        assert await queued(engine) == []
        async with engine.connect() as connection:
            kept = (await connection.execute(select(INGEST_REJECTIONS.c.reason))).scalars().all()
        assert sorted(kept) == sorted(reasons)

    with_route(scenario)


def test_a_batch_must_be_an_alarm_list_of_a_sensible_size() -> None:
    async def scenario(engine: AsyncEngine, post: Any) -> None:
        assert (await post({"samples": []})).json()["code"] == "no_alarms"
        too_many = await post({"alarms": [report()] * (MAXIMUM_ALARMS + 1)})
        assert too_many.status_code == 413

    with_route(scenario)
