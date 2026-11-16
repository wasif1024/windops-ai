"""The facts an estimate rests on, gathered from their owners as the service itself (ADR 0008).

A caller supplies only the action and the downtime. The case comes from Investigation, the
turbine, its model and its site from Asset, and the wind and power from Telemetry. An owner that
cannot be reached means no estimate, never a guess (SRS 7.7).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from windops.asset.v1 import asset_pb2
from windops.investigation.v1 import investigation_pb2
from windops.telemetry.v1 import telemetry_pb2
from windops_grpc import NotFoundError, ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

from windops_production_impact.formulas import Sample

DEADLINE_SECONDS = 3.0
# Six hours of 1 Hz wind and power is a long call, not a slow one.
STREAM_DEADLINE_SECONDS = 30.0


async def ask(client: ServiceClient, method: Any, request: Message, who: str,
              deadline: float = DEADLINE_SECONDS) -> Any:  # fmt: skip
    try:
        return await client.call(method, request, idempotent=True, deadline_seconds=deadline)
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError(f"{who} has no such record") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


def _collected(method: Callable[..., Any]) -> Callable[..., Awaitable[list[Any]]]:
    """A stream as one awaited call, so it passes the same breaker and deadline as any other."""

    async def collect(request: Any, *, timeout: float, metadata: Any) -> list[Any]:
        return [item async for item in method(request, timeout=timeout, metadata=metadata)]

    return collect


def stamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value)
    return result


class Sources(Protocol):
    async def case(self, case_id: str) -> Any: ...
    async def turbine(self, turbine_id: str) -> Any: ...
    async def wind_farm(self, wind_farm_id: str) -> Any: ...
    async def samples(self, turbine_id: str, start: datetime, end: datetime) -> list[Sample]: ...


class ServiceSources:
    def __init__(self, stubs: dict[str, Any], clients: dict[str, ServiceClient]) -> None:
        self._stubs, self._clients = stubs, clients

    async def case(self, case_id: str) -> Any:
        request = investigation_pb2.GetCaseRequest(case_id=case_id)
        return (await ask(self._clients["investigation"], self._stubs["investigation"].GetCase,
                          request, "The Investigation Service")).case  # fmt: skip

    async def turbine(self, turbine_id: str) -> Any:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        return (await ask(self._clients["asset"], self._stubs["asset"].GetTurbine, request,
                          "The Asset Service")).turbine  # fmt: skip

    async def wind_farm(self, wind_farm_id: str) -> Any:
        request = asset_pb2.GetWindFarmRequest(wind_farm_id=wind_farm_id)
        return (await ask(self._clients["asset"], self._stubs["asset"].GetWindFarm, request,
                          "The Asset Service")).wind_farm  # fmt: skip

    async def samples(self, turbine_id: str, start: datetime, end: datetime) -> list[Sample]:
        request = telemetry_pb2.StreamTurbineTelemetryRequest(
            turbine_id=turbine_id, from_time=stamp(start), to_time=stamp(end),
            metrics=[telemetry_pb2.METRIC_WIND_SPEED, telemetry_pb2.METRIC_POWER],
        )  # fmt: skip
        points = await ask(
            self._clients["telemetry"],
            _collected(self._stubs["telemetry"].StreamTurbineTelemetry), request,
            "The Telemetry Service", STREAM_DEADLINE_SECONDS,
        )  # fmt: skip
        return pair(points)


def pair(points: list[Any]) -> list[Sample]:
    """Wind and power at the same moment, leaving out any reading marked missing."""
    by_time: dict[datetime, dict[int, float]] = {}
    for point in points:
        if point.quality == telemetry_pb2.QUALITY_MISSING:
            continue
        at = point.event_time.ToDatetime(tzinfo=UTC)
        by_time.setdefault(at, {})[point.metric] = point.value
    wind, power = telemetry_pb2.METRIC_WIND_SPEED, telemetry_pb2.METRIC_POWER
    return [
        Sample(at, readings[wind], readings[power])
        for at, readings in sorted(by_time.items())
        if wind in readings and power in readings
    ]
