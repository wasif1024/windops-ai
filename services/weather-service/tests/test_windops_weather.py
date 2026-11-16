"""The weather API through a real gRPC server and SQL database, with stand-ins for Asset and the
provider. SQLite has no row-level security or triggers, so `make weather-check` proves those."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.asset.v1 import asset_pb2
from windops.weather.v1 import weather_pb2 as pb
from windops.weather.v1 import weather_pb2_grpc
from windops_grpc import ServiceRuntime, Settings, VerifiedIdentity, current_identity, sign_identity
from windops_weather import provider
from windops_weather.service import WeatherService
from windops_weather.store import METADATA, SERVICE_NAME

SECRET = "x" * 32
TENANT = "north-sea"
SETTINGS = Settings(
    environment="test", version="test", identity_secret=SECRET, grpc_port=0, http_port=0,
    shutdown_grace_seconds=1,
)  # fmt: skip
START = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def stamp(at: datetime) -> Timestamp:
    value = Timestamp()
    value.FromDatetime(at)
    return value


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeAuthorizer:
    grants: set[tuple[str, str]] = field(default_factory=set)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        who = current_identity().user_id or current_identity().subject
        return (who, permission) in self.grants or (
            bool(wind_farm_id) and (who, f"{permission}@{wind_farm_id}") in self.grants
        )


class FakeSources:
    asked_as: list[str]

    def __init__(self) -> None:
        self.asked_as = []

    async def turbine(self, turbine_id: str) -> Any:
        self.asked_as.append(current_identity().subject)
        return asset_pb2.Turbine(
            turbine_id=turbine_id, wind_farm_id="dogger-east",
            location=asset_pb2.Location(latitude=54.81234, longitude=1.90678),
            model=asset_pb2.TurbineModel(hub_height_m=100.0),
        )  # fmt: skip


@dataclass
class FakeProvider:
    """Wind rising 1 m/s an hour from 8 m/s at 10 m, for three days from the morning asked."""

    down: bool = False
    calls: int = 0

    async def fetch(self, latitude: float, longitude: float, now: datetime) -> Any:
        self.calls += 1
        if self.down:
            raise provider.ProviderUnavailableError("The provider cannot be reached right now")
        base = now.replace(minute=0, second=0, microsecond=0)
        hours = tuple(
            provider.Hour(base + timedelta(hours=i), {
                "wind_speed_10m": 8.0 + i, "wind_speed_80m": 10.0 + i,
                "wind_speed_120m": 12.0 + i, "wind_gusts_10m": 12.0 + i,
                "temperature_2m": 9.0, "precipitation": 0.5,
            })
            for i in range(72)
        )  # fmt: skip
        lat, lon = provider.rounded(latitude, longitude)
        return provider.Retrieval(lat, lon, 0.0, now, hours, "a" * 64)


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    clock: Clock
    provider: FakeProvider
    sources: FakeSources


def as_user(user: str = "u-operator") -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=TENANT, user_id=user,
                                auth_level="password", auth_time=int(time.time()))  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def forecast(world: World, user: str = "u-operator", hours: int = 6) -> Any:
    now = world.clock.now
    request = pb.GetForecastRequest(turbine_id="dogger-east-t01", from_time=stamp(now),
                                    to_time=stamp(now + timedelta(hours=hours)))  # fmt: skip
    return await world.stub.GetForecast(request, metadata=as_user(user), timeout=5)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


def with_service(scenario: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine_.begin() as connection:
            await connection.run_sync(METADATA.create_all)
        authorizer = FakeAuthorizer({("u-operator", "weather.read"),
                                     ("u-dogger", "weather.read@dogger-east")})  # fmt: skip
        clock, fake, sources = Clock(), FakeProvider(), FakeSources()

        def register(server: grpc.aio.Server) -> None:
            weather_pb2_grpc.add_WeatherServiceServicer_to_server(
                WeatherService(engine_, authorizer, sources, fake, SECRET, clock=clock), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = weather_pb2_grpc.WeatherServiceStub(channel)
                await scenario(World(stub, engine_, clock, fake, sources))
        finally:
            await runtime.stop()
            await engine_.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_forecast_keeps_its_provenance_and_only_what_the_provider_gave() -> None:
    async def scenario(world: World) -> None:
        answer = await forecast(world)
        assert (answer.state, answer.age_seconds) == ("fresh", 0)
        f = answer.forecast
        assert (f.provider, f.licence, f.latitude, f.longitude, f.hub_height_m) == (
            "open-meteo", "CC BY 4.0", 54.81, 1.91, 100.0,
        )  # fmt: skip
        assert f.forecast_id.startswith("fct_") and f.content_hash == "a" * 64
        assert len(answer.points) == 6
        first = answer.points[0]
        assert first.wind_speed_hub_mps == 11.0  # Halfway from 80 m to 120 m.
        assert not first.HasField("visibility_m"), "the provider gave no visibility"
        summary = answer.summary
        assert (summary.max_wind_speed_hub_mps, summary.max_wind_gusts_10m_mps) == (16.0, 17.0)
        assert not summary.HasField("min_visibility_m")
        assert (summary.hours_covered, summary.hours_wanted) == (6, 6)
        assert summary.total_precipitation_mm == 3.0
        assert world.sources.asked_as == ["service:weather"]

    with_service(scenario)


def test_a_kept_forecast_is_used_for_an_hour_then_asked_for_again() -> None:
    async def scenario(world: World) -> None:
        await forecast(world)
        world.clock.now += timedelta(minutes=59)
        cached = await forecast(world)
        assert (world.provider.calls, cached.state, cached.age_seconds) == (1, "fresh", 3540)
        world.clock.now += timedelta(minutes=2)
        renewed = await forecast(world)
        assert (world.provider.calls, renewed.age_seconds) == (2, 0)

    with_service(scenario)


def test_during_an_outage_old_data_is_marked_stale_and_never_presented_as_current() -> None:
    async def scenario(world: World) -> None:
        world.provider.down = True
        none = await forecast(world)
        assert (none.state, none.HasField("forecast"), len(none.points)) == (
            "unavailable", False, 0,
        )  # fmt: skip
        assert "cannot be reached" in none.note
        world.provider.down = False
        await forecast(world)
        world.provider.down = True
        world.clock.now += timedelta(hours=2)
        recent = await forecast(world)
        assert recent.state == "fresh" and "cannot be reached" in recent.note
        world.clock.now += timedelta(hours=1, minutes=1)
        old = await forecast(world)
        assert old.state == "stale" and old.age_seconds == 3 * 3600 + 60
        assert "cannot be reached" in old.note and "more than 3 h" in old.note

    with_service(scenario)


def test_a_window_past_the_forecast_is_covered_only_as_far_as_it_goes() -> None:
    async def scenario(world: World) -> None:
        answer = await forecast(world, hours=24 * 7)
        assert (answer.summary.hours_covered, answer.summary.hours_wanted) == (72, 168)
        too_far = forecast(world, hours=24 * 8)
        assert await code_of(too_far) == grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_someone_without_weather_read_cannot_find_the_turbine_and_a_site_grant_is_enough() -> None:
    async def scenario(world: World) -> None:
        assert await code_of(forecast(world, user="u-nobody")) == grpc.StatusCode.NOT_FOUND
        assert (await forecast(world, user="u-dogger")).state == "fresh"

    with_service(scenario)
