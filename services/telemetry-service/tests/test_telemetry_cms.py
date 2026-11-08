"""CMS vibration: spectral features, fault signatures and the /cms device route (FR-TEL-004)."""

import asyncio
import cmath
import json
import math
import random
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops_simulator import cms
from windops_simulator.faults import FaultSpec
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.simulator import Simulator
from windops_telemetry import credentials, spectra
from windops_telemetry.ingest import PREFIX, create_ingest_router
from windops_telemetry.limits import DeviceLimiter
from windops_telemetry.store import (
    CMS_FEATURES,
    CMS_WAVEFORMS,
    DEVICE_CREDENTIALS,
    INGEST_REJECTIONS,
    METADATA,
)

TURBINE = "hornsea-west-t02"
START = datetime.fromisoformat("2026-01-15T06:00:00+00:00")
URL = f"{PREFIX}/cms"


def batch(
    faults: tuple[FaultSpec, ...] = (), at: datetime = START, turbine: str = TURBINE
) -> dict[str, Any]:
    """A CMS batch from the simulator, two minutes into a run so the rotor is turning."""
    spec = next(spec for spec in DEMO_TURBINES if spec.turbine_id == turbine)
    simulator = Simulator(1002, (spec,), at - timedelta(seconds=120), faults)
    for _ in range(120):
        simulator.step(1.0)
    return simulator.cms_batch(turbine)


def featured(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        window["sensor"]: spectra.features(
            window["values"], document["sample_rate_hz"], window["sensor"],
            document["rotor_speed_rpm"], document["generator_speed_rpm"],
        )
        for window in document["sensors"]
    }  # fmt: skip


WORN = (FaultSpec("bearing_wear", TURBINE, timedelta(0), timedelta(seconds=1),
                  component="main_bearing"),)  # fmt: skip
IMBALANCED = (FaultSpec("imbalance", TURBINE, timedelta(0), timedelta(seconds=1)),)


# --- the transform and the features


def test_the_fft_matches_the_definition_and_keeps_energy() -> None:
    rng = random.Random(3)  # noqa: S311  # Test data, not secrets.
    values = [rng.gauss(0, 1) for _ in range(256)]
    naive = [
        sum(values[j] * cmath.exp(-2j * math.pi * k * j / 256) for j in range(256))
        for k in range(256)
    ]
    assert max(abs(a - b) for a, b in zip(spectra.fft(values), naive, strict=True)) < 1e-9
    energy = sum(abs(c) ** 2 for c in spectra.fft(values)) / 256
    assert energy == pytest.approx(sum(v * v for v in values))
    with pytest.raises(ValueError, match="power of two"):
        spectra.fft([1.0, 2.0, 3.0])


def test_a_tone_shows_its_rms_in_its_band_and_nowhere_else() -> None:
    rate, n = 5120, 8192
    tone = [0.3 * math.sin(2 * math.pi * 103.7 * i / rate) for i in range(n)]
    found = spectra.features(tone, rate, "generator", 10.0, 1000.0)
    assert found["band_broadband_rms"] == pytest.approx(0.3 / math.sqrt(2), rel=1e-3)
    assert found["rms"] == pytest.approx(0.3 / math.sqrt(2), rel=1e-3)
    assert (found["band_high_rms"] or 0.0) < 1e-3
    assert found["crest_factor"] == pytest.approx(math.sqrt(2), rel=1e-2)
    assert found["kurtosis"] == pytest.approx(1.5, rel=1e-2)  # a sine's


def test_a_stopped_shaft_has_no_shaft_bands_and_gear_mesh_is_only_on_the_gearbox() -> None:
    noise = [random.Random(1).gauss(0, 0.01) for _ in range(1024)]  # noqa: S311
    stopped = spectra.features(noise, 5120, "gearbox", 0.0, 0.0)
    assert stopped["band_shaft_rms"] is stopped["band_bearing_rms"] is None
    assert stopped["band_gear_mesh_rms"] is None
    turning = featured(batch())
    assert turning["gearbox"]["band_gear_mesh_rms"] is not None
    assert turning["generator"]["band_gear_mesh_rms"] is None


def test_the_kinematics_are_the_simulators() -> None:
    for sensor in cms.SENSORS:
        kinematics = spectra.KINEMATICS[sensor.name]
        assert (kinematics.shaft, kinematics.bearing_order, kinematics.gear_teeth) == (
            sensor.shaft, sensor.bearing_order, sensor.gear_teeth
        )  # fmt: skip
    assert spectra.GEARBOX_RATIO == 100.0


def test_a_worn_main_bearing_shows_as_impulsiveness_on_its_own_sensor_only() -> None:
    """A slow bearing: few impacts in a window, so kurtosis rises far more than energy does."""
    healthy, worn = featured(batch()), featured(batch(WORN))
    bearing, before = worn["main_bearing"], healthy["main_bearing"]
    assert bearing["kurtosis"] > 5 * before["kurtosis"]
    assert bearing["band_high_rms"] > 1.3 * before["band_high_rms"]
    assert bearing["band_bearing_rms"] > before["band_bearing_rms"]
    assert worn["generator"]["kurtosis"] == pytest.approx(healthy["generator"]["kurtosis"], rel=0.2)


@pytest.mark.parametrize(("component", "sensor"), [("gearbox_bearing", "gearbox"),
                                                   ("generator_bearing", "generator")])  # fmt: skip
def test_a_worn_fast_bearing_shows_as_high_frequency_energy(component: str, sensor: str) -> None:
    """A fast bearing: many impacts ringing at the resonance, well above the healthy level."""
    worn = (FaultSpec("bearing_wear", TURBINE, timedelta(0), timedelta(seconds=1),
                      component=component),)  # fmt: skip
    before, after = featured(batch())[sensor], featured(batch(worn))[sensor]
    assert after["band_high_rms"] > 5 * before["band_high_rms"]
    assert after["kurtosis"] > 3 * before["kurtosis"]


def test_rotor_imbalance_raises_the_main_bearings_shaft_band() -> None:
    healthy = featured(batch())["main_bearing"]["band_shaft_rms"]
    imbalanced = featured(batch(IMBALANCED))["main_bearing"]["band_shaft_rms"]
    assert healthy is not None and imbalanced is not None
    assert imbalanced > 3 * healthy


# --- the route


@dataclass
class World:
    client: httpx.AsyncClient
    engine: AsyncEngine
    token: str

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def rows(self, table: Any) -> list[Any]:
        async with self.engine.connect() as connection:
            return list((await connection.execute(select(table))).all())


class Counts:
    async def incrby(self, name: str, amount: int) -> int:
        return 1

    async def expire(self, name: str, time: int) -> bool:
        return True


def with_route(scenario: Callable[[World], Awaitable[None]], now: datetime = START) -> None:
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
        device_id, secret = credentials.new_device_id(), credentials.new_secret()
        async with engine.begin() as connection:
            await connection.execute(
                DEVICE_CREDENTIALS.insert().values(
                    tenant_id="north-sea", device_id=device_id, turbine_id=TURBINE,
                    secret_hash=credentials.hashed(secret), created_by="u", created_at=START,
                )
            )  # fmt: skip
        app = FastAPI()
        app.include_router(
            create_ingest_router(
                engine, DeviceLimiter(Counts()), clock=lambda: now + timedelta(seconds=2)
            )
        )
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="https://ingest") as client:
                await scenario(World(client, engine, credentials.token("north-sea", device_id,
                                                                       secret)))  # fmt: skip
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_a_batch_becomes_features_for_each_sensor_and_its_raw_window_on_schedule() -> None:
    """At 06:00, the first window of a 6-hour block, so its raw values are kept."""

    async def scenario(world: World) -> None:
        sent = batch()
        response = await world.client.post(URL, json=sent, headers=world.auth())
        assert response.json() == {"accepted": 3, "rejected": [], "duplicates": 0}
        rows = {row.sensor: row for row in await world.rows(CMS_FEATURES)}
        assert set(rows) == {"main_bearing", "gearbox", "generator"}
        gearbox = rows["gearbox"]
        assert (gearbox.feature_version, gearbox.samples, gearbox.sample_rate_hz) == (
            "1",
            8192,
            5120,
        )
        assert gearbox.synthetic is True and gearbox.window_id
        assert gearbox.band_gear_mesh_rms == pytest.approx(
            featured(sent)["gearbox"]["band_gear_mesh_rms"]
        )
        waves = {row.sensor: row for row in await world.rows(CMS_WAVEFORMS)}
        assert waves["gearbox"].kept_for == "schedule"
        assert waves["gearbox"].window_id == gearbox.window_id
        values = struct.unpack("<8192f", waves["gearbox"].values_f32le)
        original = sent["sensors"][1]["values"]
        assert max(abs(a - b) for a, b in zip(values, original, strict=True)) < 1e-6

        again = await world.client.post(URL, json=sent, headers=world.auth())
        assert again.json() == {"accepted": 0, "rejected": [], "duplicates": 3}

    with_route(scenario)


def test_between_schedules_only_features_are_kept_unless_the_device_triggers() -> None:
    later = START + timedelta(minutes=10)

    async def scenario(world: World) -> None:
        await world.client.post(URL, json=batch(at=later), headers=world.auth())
        assert await world.rows(CMS_WAVEFORMS) == []
        assert all(row.window_id is None for row in await world.rows(CMS_FEATURES))
        flagged = {**batch(at=later + timedelta(minutes=10)), "trigger": "rms_above_baseline"}
        await world.client.post(URL, json=flagged, headers=world.auth())
        kept = await world.rows(CMS_WAVEFORMS)
        assert {row.kept_for for row in kept} == {"trigger:rms_above_baseline"}
        assert len(kept) == 3

    with_route(scenario, now=later + timedelta(minutes=10))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"turbine_id": "hornsea-west-t01"}, "cms_wrong_turbine"),
        ({"tenant_id": "atlas-wind"}, "cms_wrong_tenant"),
        ({"schema_version": "9"}, "cms_unknown_schema_version"),
        ({"sample_rate_hz": 12}, "cms_bad_sample_rate_hz"),
        ({"event_time": "2026-01-15T09:00:00+00:00"}, "cms_event_time_in_future"),
        ({"sensors": [{"sensor": "tower", "unit": "g", "values": [0.0] * 1024}]},
         "cms_unknown_sensor"),
        ({"sensors": [{"sensor": "gearbox", "unit": "g", "values": [0.0] * 1000}]},
         "cms_bad_window_length"),
        ({"sensors": [{"sensor": "gearbox", "unit": "mm/s", "values": [0.0] * 1024}]},
         "cms_bad_unit"),
    ],
)  # fmt: skip
def test_a_bad_batch_is_refused_and_kept_with_its_reason(
    change: dict[str, Any], reason: str
) -> None:
    """FR-TEL-003, without the long windows in the excerpt."""

    async def scenario(world: World) -> None:
        response = await world.client.post(URL, json={**batch(), **change}, headers=world.auth())
        assert response.json()["rejected"] == [{"index": 0, "reason": reason}]
        (kept,) = await world.rows(INGEST_REJECTIONS)
        assert kept.reason == reason
        assert '"values"' not in kept.excerpt and len(kept.excerpt) <= 500
        assert await world.rows(CMS_FEATURES) == []

    with_route(scenario)


def test_the_route_needs_a_device_credential_and_json() -> None:
    async def scenario(world: World) -> None:
        refused = await world.client.post(URL, json=batch())
        assert refused.status_code == 401
        broken = await world.client.post(URL, content=b"{", headers=world.auth())
        assert (broken.status_code, broken.json()["code"]) == (400, "not_json")
        assert json.dumps(batch()).count("values") == 3

    with_route(scenario)
