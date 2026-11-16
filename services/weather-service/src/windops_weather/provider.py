"""The Open-Meteo adapter (WTH-001, ADR 0010): one request, normalized to forecast hours.

Only rounded coordinates leave the cluster, never a tenant, turbine or case (EXT-006), and each
request is logged without them (EGR-004). TLS is always verified. A 429, a 5xx, a timeout or no
connection counts against the provider's circuit breaker; anything else it says is a refusal.
"""

import hashlib
import itertools
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from windops_resilience import BreakerOpenError, CircuitBreaker

logger = logging.getLogger("windops.service")
PROVIDER = "open-meteo"
LICENCE = "CC BY 4.0"
# The provider's own choice of model for the location.
MODEL = "best_match"
# About a kilometre: enough for a forecast, too coarse to name a turbine (EXT-006).
COORDINATE_DECIMALS = 2
PAST_DAYS, FORECAST_DAYS = 2, 7
TIMEOUT_SECONDS = 10.0
# The levels the provider gives wind at, for the hub's interpolation.
WIND_LEVELS = (10, 80, 120, 180)
HOURLY = (
    "wind_speed_10m", "wind_speed_80m", "wind_speed_120m", "wind_speed_180m",
    "wind_gusts_10m", "wind_direction_10m", "temperature_2m", "precipitation", "visibility",
    "cape",
)  # fmt: skip


class ProviderUnavailableError(Exception):
    """The provider cannot be reached, or did not answer usably."""


@dataclass(frozen=True)
class Hour:
    valid_at: datetime
    values: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Retrieval:
    latitude: float
    longitude: float
    elevation_m: float
    retrieved_at: datetime
    hours: tuple[Hour, ...]
    content_hash: str
    model: str = MODEL
    # Open-Meteo's forecast does not say which model run it comes from.
    revision: str = ""


def rounded(latitude: float, longitude: float) -> tuple[float, float]:
    return round(latitude, COORDINATE_DECIMALS), round(longitude, COORDINATE_DECIMALS)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def hub_wind(values: dict[str, float], hub_height_m: float) -> float | None:
    """Linear between the provider's two levels around the hub, when it gives both."""
    levels = [(h, values.get(f"wind_speed_{h}m")) for h in WIND_LEVELS]
    known = [(h, v) for h, v in levels if v is not None]
    for (low, low_value), (high, high_value) in itertools.pairwise(known):
        if low <= hub_height_m <= high:
            return low_value + (high_value - low_value) * (hub_height_m - low) / (high - low)
    return None


def parse(body: dict[str, Any], retrieved_at: datetime, raw: bytes) -> Retrieval:
    """The provider's hourly arrays, as hours holding only the values it supplied (WTH-004)."""
    hourly = body.get("hourly")
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise ProviderUnavailableError("The provider's answer has no hourly forecast")
    times = hourly["time"]
    hours = []
    for index, stamp in enumerate(times):
        try:
            valid_at = datetime.fromisoformat(str(stamp)).replace(tzinfo=UTC)
        except ValueError as error:
            raise ProviderUnavailableError(
                "The provider's answer has an unreadable time"
            ) from error
        values = {}
        for name in HOURLY:
            column = hourly.get(name)
            if isinstance(column, list) and index < len(column):
                value = _number(column[index])
                if value is not None:
                    values[name] = value
        hours.append(Hour(valid_at, values))
    latitude, longitude = _number(body.get("latitude")), _number(body.get("longitude"))
    if latitude is None or longitude is None:
        raise ProviderUnavailableError("The provider's answer has no coordinates")
    return Retrieval(
        latitude=latitude, longitude=longitude, elevation_m=_number(body.get("elevation")) or 0.0,
        retrieved_at=retrieved_at, hours=tuple(hours),
        content_hash=hashlib.sha256(raw).hexdigest(),
    )  # fmt: skip


class OpenMeteo:
    def __init__(self, url: str, breaker: CircuitBreaker, http: httpx.AsyncClient) -> None:
        self._url, self._breaker, self._http = url, breaker, http

    async def fetch(self, latitude: float, longitude: float, now: datetime) -> Retrieval:
        lat, lon = rounded(latitude, longitude)
        params = {
            "latitude": f"{lat:.{COORDINATE_DECIMALS}f}",
            "longitude": f"{lon:.{COORDINATE_DECIMALS}f}",
            "hourly": ",".join(HOURLY), "wind_speed_unit": "ms", "timezone": "GMT",
            "past_days": str(PAST_DAYS), "forecast_days": str(FORECAST_DAYS),
        }  # fmt: skip

        async def send() -> httpx.Response:
            try:
                response = await self._http.get(self._url, params=params, timeout=TIMEOUT_SECONDS)
            except httpx.TimeoutException as error:
                raise TimeoutError("The provider did not answer in time") from error
            except httpx.TransportError as error:
                raise ConnectionError("The provider cannot be reached") from error
            if response.status_code == 429 or response.status_code >= 500:
                # Counted by the breaker as the provider failing, not as a refusal.
                response.raise_for_status()
            return response

        # Only the rounded coordinates went out (EGR-004).
        logger.info("Weather request sent", extra={"provider": PROVIDER, "latitude": lat,
                                                   "longitude": lon})  # fmt: skip
        try:
            response = await self._breaker.call(send)
        except (BreakerOpenError, TimeoutError, ConnectionError, httpx.HTTPError) as error:
            raise ProviderUnavailableError("The provider cannot be reached right now") from error
        if response.status_code != 200:
            raise ProviderUnavailableError(
                f"The provider refused the request: {response.status_code}"
            )
        try:
            body = json.loads(response.content)
        except ValueError as error:
            raise ProviderUnavailableError("The provider's answer is not JSON") from error
        if not isinstance(body, dict):
            raise ProviderUnavailableError("The provider's answer is not an object")
        return parse(body, now, response.content)
