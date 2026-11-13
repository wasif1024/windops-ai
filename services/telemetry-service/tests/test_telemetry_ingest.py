"""The device route, through a real ASGI app and SQL database, with samples from the simulator.

SQLite has no row-level security or partitions, so `make telemetry-check` proves those against
PostgreSQL.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops_events import parse_event
from windops_outbox import OUTBOX_TABLE, create_outbox_table
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.simulator import Simulator
from windops_telemetry import credentials
from windops_telemetry.ingest import MAXIMUM_SAMPLES, PREFIX, create_ingest_router
from windops_telemetry.limits import DeviceLimiter
from windops_telemetry.schema import MalformedError, parse
from windops_telemetry.store import (
    DEVICE_CREDENTIALS,
    INGEST_REJECTIONS,
    METADATA,
    OPERATIONAL_SAMPLES,
)

TENANT = "north-sea"
TURBINE = "dogger-east-t01"
START = datetime.fromisoformat("2026-01-15T06:00:00+00:00")
RECEIVED = datetime.fromisoformat("2026-01-15T06:00:05+00:00")
URL = f"{PREFIX}/samples"


def samples(count: int = 3, turbine_id: str = TURBINE) -> list[dict[str, Any]]:
    """What the simulator's device for one turbine sends, one a second."""
    spec = next(spec for spec in DEMO_TURBINES if spec.turbine_id == turbine_id)
    simulator = Simulator(7, (spec,), START)
    return [dict(simulator.step(1.0)[0].as_dict()) for _ in range(count)]


@dataclass
class FakeRedis:
    counts: dict[str, int] = field(default_factory=dict)
    broken: bool = False

    async def incrby(self, name: str, amount: int) -> int:
        if self.broken:
            raise ConnectionError("down")
        self.counts[name] = self.counts.get(name, 0) + amount
        return self.counts[name]

    async def expire(self, name: str, time: int) -> bool:
        return True


@dataclass
class World:
    client: httpx.AsyncClient
    engine: AsyncEngine
    redis: FakeRedis
    token: str

    def auth(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    async def rows(self, table: Any) -> list[Any]:
        async with self.engine.connect() as connection:
            return list((await connection.execute(select(table))).all())


async def add_credential(
    engine: AsyncEngine, tenant: str = TENANT, turbine: str = TURBINE, *, revoked: bool = False
) -> str:
    device_id, secret = credentials.new_device_id(), credentials.new_secret()
    async with engine.begin() as connection:
        await connection.execute(
            DEVICE_CREDENTIALS.insert().values(
                tenant_id=tenant,
                device_id=device_id,
                turbine_id=turbine,
                secret_hash=credentials.hashed(secret),
                created_by="u-admin",
                created_at=START,
                revoked_at=START if revoked else None,
            )
        )
    return credentials.token(tenant, device_id, secret)


def with_route(scenario: Callable[[World], Awaitable[None]], *, limit: int = 600) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        token = await add_credential(engine)
        redis = FakeRedis()
        # A clock in the middle of a minute, so the window never turns over during a test.
        limiter = DeviceLimiter(redis, limit=limit, clock=lambda: 1_800_000_030.0)
        app = FastAPI()
        app.include_router(create_ingest_router(engine, limiter, clock=lambda: RECEIVED))
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="https://ingest") as client:
                await scenario(World(client, engine, redis, token))
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_a_device_sends_simulator_samples_which_are_stored_with_both_times() -> None:
    """FR-TEL-001, FR-TEL-002: event and ingestion time, source, schema and quality status."""

    async def scenario(world: World) -> None:
        sent = samples(3)
        response = await world.client.post(URL, json={"samples": sent}, headers=world.auth())
        assert response.status_code == 200
        assert response.json() == {"accepted": 3, "rejected": [], "duplicates": 0}
        rows = await world.rows(OPERATIONAL_SAMPLES)
        assert [row.sequence for row in rows] == [1, 2, 3]
        first = rows[0]
        assert (first.tenant_id, first.turbine_id) == (TENANT, TURBINE)
        assert first.event_time.replace(tzinfo=UTC) == datetime.fromisoformat(sent[0]["event_time"])
        assert first.ingested_at.replace(tzinfo=UTC) == RECEIVED
        assert (first.schema_version, first.quality_status, first.synthetic) == (
            "2",
            "received",
            True,
        )
        assert first.power_kw == pytest.approx(sent[0]["power_kw"])
        assert first.source == sent[0]["source"]

    with_route(scenario)


def test_every_bad_credential_gets_the_same_answer() -> None:
    """FR-TEL-005: nothing tells a guesser which part was wrong."""

    async def scenario(world: World) -> None:
        tenant, device, _ = world.token.split(".")
        revoked = await add_credential(world.engine, revoked=True)
        tries: list[dict[str, str]] = [
            {},
            {"Authorization": "Basic abc"},
            world.auth("not-a-token"),
            world.auth(f"{tenant}.{device}.{'A' * 43}"),
            world.auth(f"atlas-wind.{device}.{world.token.split('.')[2]}"),
            world.auth(revoked),
        ]
        for headers in tries:
            response = await world.client.post(URL, json={"samples": []}, headers=headers)
            assert response.status_code == 401, headers
            assert response.json()["code"] == "bad_credential"
            assert response.headers["WWW-Authenticate"] == "Bearer"
        assert await world.rows(OPERATIONAL_SAMPLES) == []

    with_route(scenario)


def test_a_device_can_send_only_its_own_turbines_samples() -> None:
    """FR-TEL-005: a credential is for one tenant and turbine. Refusals are kept, with reasons."""

    async def scenario(world: World) -> None:
        good = samples(1)[0]
        other_turbine = samples(1, "dogger-east-t02")[0]
        other_tenant = {**good, "tenant_id": "atlas-wind"}
        response = await world.client.post(
            URL, json={"samples": [good, other_turbine, other_tenant]}, headers=world.auth()
        )
        assert response.json() == {
            "accepted": 1,
            "rejected": [
                {"index": 1, "reason": "wrong_turbine"},
                {"index": 2, "reason": "wrong_tenant"},
            ],
            "duplicates": 0,
        }
        rejections = await world.rows(INGEST_REJECTIONS)
        assert sorted(row.reason for row in rejections) == ["wrong_tenant", "wrong_turbine"]
        assert all(row.turbine_id == TURBINE for row in rejections)

    with_route(scenario)


def test_malformed_samples_are_rejected_with_reasons_and_never_dropped() -> None:
    """FR-TEL-003: the simulator's three malformations, each kept with an excerpt."""

    async def scenario(world: World) -> None:
        good = samples(1)[0]
        missing_time = {k: v for k, v in good.items() if k != "event_time"}
        wrong_type = {**good, "power_kw": f"{good['power_kw']}kW"}
        cut_off = json.dumps(good)[:40]
        response = await world.client.post(
            URL,
            json={"samples": [missing_time, wrong_type, cut_off, good]},
            headers=world.auth(),
        )
        assert response.json() == {
            "accepted": 1,
            "rejected": [
                {"index": 0, "reason": "bad_event_time"},
                {"index": 1, "reason": "bad_power_kw"},
                {"index": 2, "reason": "not_json"},
            ],
            "duplicates": 0,
        }
        rejections = {row.reason: row for row in await world.rows(INGEST_REJECTIONS)}
        assert rejections["not_json"].excerpt == cut_off
        assert len(rejections["bad_power_kw"].excerpt) <= 500

    with_route(scenario)


def test_a_body_that_cannot_be_a_batch_is_refused_whole() -> None:
    async def scenario(world: World) -> None:
        auth = world.auth()
        refused = [
            (await world.client.post(URL, content=b"{not json", headers=auth), 400, "not_json"),
            (await world.client.post(URL, json=[1, 2], headers=auth), 400, "no_samples"),
            (
                await world.client.post(
                    URL, json={"samples": [{}] * (MAXIMUM_SAMPLES + 1)}, headers=auth
                ),
                413,
                "too_many_samples",
            ),
            (
                await world.client.post(URL, content=b" " * (1024 * 1024 + 1), headers=auth),
                413,
                "too_large",
            ),
        ]
        for response, status, code in refused:
            assert (response.status_code, response.json()["code"]) == (status, code)
            assert response.headers["content-type"] == "application/problem+json"
        assert await world.rows(INGEST_REJECTIONS) == []

    with_route(scenario)


def test_each_device_has_its_own_rate_limit_and_it_fails_closed() -> None:
    """SRS 7.5: per device, and refused when the counters cannot be reached."""

    async def scenario(world: World) -> None:
        other = await add_credential(world.engine, turbine="dogger-east-t02")
        for _ in range(2):
            ok = await world.client.post(URL, json={"samples": []}, headers=world.auth())
            assert ok.status_code == 200
        limited = await world.client.post(URL, json={"samples": []}, headers=world.auth())
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "30"
        other_device = await world.client.post(URL, json={"samples": []}, headers=world.auth(other))
        assert other_device.status_code == 200
        world.redis.broken = True
        down = await world.client.post(URL, json={"samples": []}, headers=world.auth(other))
        assert (down.status_code, down.json()["code"]) == (503, "unavailable")

    with_route(scenario, limit=2)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"schema_version": "1"}, "unknown_schema_version"),
        ({"sequence": 0}, "bad_sequence"),
        ({"sequence": True}, "bad_sequence"),
        ({"event_time": "2026-01-15T06:00:00"}, "bad_event_time"),
        ({"wind_speed_mps": False}, "bad_wind_speed_mps"),
        ({"synthetic": "yes"}, "bad_synthetic"),
        ({"turbine_id": "x" * 65}, "bad_turbine_id"),
        ({"source": ""}, "bad_source"),
    ],
)
def test_the_schema_names_what_is_wrong(change: dict[str, Any], reason: str) -> None:
    with pytest.raises(MalformedError) as raised:
        parse({**samples(1)[0], **change})
    assert raised.value.reason == reason


def test_a_null_reading_is_kept_as_null() -> None:
    """A sensor that sends nothing (sensor_dropout) is not a malformed sample."""
    parsed = parse({**samples(1)[0], "main_bearing_temp_c": None})
    assert parsed.readings["main_bearing_temp_c"] is None


def test_a_token_carries_its_tenant_and_device_and_only_its_hash_is_kept() -> None:
    device_id, secret = credentials.new_device_id(), credentials.new_secret()
    shown = credentials.presented(f"Bearer {credentials.token(TENANT, device_id, secret)}")
    assert shown == credentials.Presented(TENANT, device_id, secret)
    assert credentials.matches(secret, credentials.hashed(secret))
    assert not credentials.matches(secret + "x", credentials.hashed(secret))
    assert secret not in credentials.hashed(secret)


def test_a_sample_timed_far_in_the_future_is_refused_with_its_reason() -> None:
    """A broken device clock must not fill the default partition."""

    async def scenario(world: World) -> None:
        sample = samples(1)[0]
        sample["event_time"] = (RECEIVED + timedelta(hours=2)).isoformat()
        response = await world.client.post(URL, json={"samples": [sample]}, headers=world.auth())
        assert response.json()["rejected"] == [{"index": 0, "reason": "event_time_in_future"}]

    with_route(scenario)


def test_a_stored_batch_tells_fault_detection_the_turbine_is_streaming() -> None:
    """telemetry.received, queued with the samples, so Fault Detection knows what to watch."""

    async def scenario(world: World) -> None:
        await world.client.post(URL, json={"samples": samples(3)}, headers=world.auth())
        await world.client.post(URL, json={"samples": ["broken"]}, headers=world.auth())
        events = [parse_event(row.subject, row.envelope) for row in await world.rows(OUTBOX_TABLE)]
        received = [e for e in events if e.subject == "telemetry.received"]
        assert len(received) == 1, "a batch with nothing stored announces nothing"
        payload: Any = received[0].payload
        assert (received[0].tenant_id, payload.turbine_id, payload.sample_count) == (
            TENANT, TURBINE, 3,
        )  # fmt: skip

    with_route(scenario)
