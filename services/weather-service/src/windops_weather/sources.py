"""The turbine a forecast is for, from the Asset Service, asked as this service (ADR 0008)."""

from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from windops.asset.v1 import asset_pb2
from windops_grpc import NotFoundError, ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

DEADLINE_SECONDS = 3.0


async def ask(client: ServiceClient, method: Any, request: Message, who: str) -> Any:
    try:
        return await client.call(method, request, idempotent=True,
                                 deadline_seconds=DEADLINE_SECONDS)  # fmt: skip
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError(f"{who} has no such record") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class Sources(Protocol):
    async def turbine(self, turbine_id: str) -> Any: ...


class ServiceSources:
    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def turbine(self, turbine_id: str) -> Any:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        return (await ask(self._client, self._stub.GetTurbine, request,
                          "The Asset Service")).turbine  # fmt: skip
