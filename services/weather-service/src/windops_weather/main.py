"""Start the Weather Service."""

import asyncio
import os
from pathlib import Path
from typing import Any

import grpc
import httpx
from opentelemetry import metrics
from sqlalchemy import text
from windops.asset.v1 import asset_pb2_grpc
from windops.authorization.v1 import authorization_pb2_grpc
from windops.weather.v1 import weather_pb2_grpc
from windops_auth import current_identity_token
from windops_grpc import (
    ByTenant,
    ReplaySettings,
    ServiceClient,
    create_channel,
    replay_ssl_context,
    run_service,
)
from windops_migrations import create_service_engine
from windops_observability import resilience_metrics
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_weather.provider import OpenMeteo
from windops_weather.service import AuthorizationClient, WeatherService
from windops_weather.sources import ServiceSources
from windops_weather.store import SERVICE_NAME

# The free endpoint, for non-commercial use (ADR 0010). A subscription changes only this.
DEFAULT_PROVIDER_URL = "https://api.open-meteo.com/v1/forecast"


class _Lazy:
    """Makes its client on first use, because a channel must be made on the running loop."""

    def __init__(self, make: Any) -> None:
        self._make, self._made = make, None

    def __getattr__(self, name: str) -> Any:
        if self._made is None:
            self._made = self._make()
        return getattr(self._made, name)


def _breaker(dependency: str, slow_call_seconds: float = 1.0) -> CircuitBreaker:
    return CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=slow_call_seconds),
        **resilience_metrics(metrics.get_meter_provider()),
    )


def _client(dependency: str) -> ServiceClient:
    # Whoever is current goes along: the caller, or this service acting as itself (ADR 0008).
    return ServiceClient(_breaker(dependency), identity_token=current_identity_token)


def main() -> None:
    engine = create_service_engine(pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])

    def stub(make: Any, setting: str) -> Any:
        return make(create_channel(os.environ[setting], tls))

    authorizer = _Lazy(lambda: AuthorizationClient(
        stub(authorization_pb2_grpc.AuthorizationServiceStub, "WINDOPS_AUTHORIZATION_ADDRESS"),
        _client("authorization"),
    ))  # fmt: skip
    sources = _Lazy(lambda: ServiceSources(
        stub(asset_pb2_grpc.AssetServiceStub, "WINDOPS_ASSET_ADDRESS"), _client("asset"),
    ))  # fmt: skip
    # TLS is verified against the system's certificate authorities, always.
    live = _Lazy(lambda: OpenMeteo(
        os.environ.get("WINDOPS_WEATHER_PROVIDER_URL", DEFAULT_PROVIDER_URL),
        _breaker("open-meteo", slow_call_seconds=5.0), httpx.AsyncClient(),
    ))  # fmt: skip
    forecasts: Any = live
    # The evaluation tenant's forecasts are recorded, so a regression is repeatable (EVL-001).
    replaying = ReplaySettings.from_env()
    if replaying is not None:
        replayed = _Lazy(lambda: OpenMeteo(
            f"{replaying.url}/open-meteo/v1/forecast",
            _breaker("open-meteo-replay", slow_call_seconds=5.0),
            httpx.AsyncClient(verify=replay_ssl_context(tls)),
        ))  # fmt: skip
        forecasts = ByTenant(live, replayed, replaying.tenants)

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    def register(server: grpc.aio.Server) -> None:
        weather_pb2_grpc.add_WeatherServiceServicer_to_server(
            WeatherService(engine, authorizer, sources, forecasts,
                           os.environ["WINDOPS_IDENTITY_SECRET"]),
            server,
        )  # fmt: skip

    asyncio.run(run_service(SERVICE_NAME, register, readiness_checks=(database_ready,)))


if __name__ == "__main__":
    main()
