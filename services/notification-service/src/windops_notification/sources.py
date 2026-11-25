"""The owners this service asks.

The dispatcher asks as this service, in the event's tenant: who holds a permission there, from
Authorization, and a turbine's wind farm, from Asset, for the scope. The API checks a caller's
own permission as the caller. Nothing falls back: what cannot be asked raises UnavailableError,
and the work is tried again later (SRS 7.7).
"""

from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from windops.asset.v1 import asset_pb2
from windops.authorization.v1 import authorization_pb2
from windops_grpc import NotFoundError, ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

DEADLINE_SECONDS = 3.0


async def ask(client: ServiceClient, method: Any, request: Message, who: str) -> Any:
    try:
        return await client.call(method, request, idempotent=True,
                                 deadline_seconds=DEADLINE_SECONDS)  # fmt: skip
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError(f"{who} has no such record for this caller") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class Directory(Protocol):
    """Asked as this service, in the tenant it acts for."""

    async def holders(self, permission: str, wind_farm_id: str, turbine_id: str) -> list[str]: ...

    async def wind_farm(self, turbine_id: str) -> str:
        """The turbine's wind farm, or "" when Asset has no such turbine."""


class Authorizer(Protocol):
    """Asked as the caller."""

    async def allowed(self, permission: str) -> bool: ...


class ServiceDirectory:
    def __init__(self, stubs: dict[str, Any], clients: dict[str, ServiceClient]) -> None:
        self._stubs, self._clients = stubs, clients

    async def holders(self, permission: str, wind_farm_id: str, turbine_id: str) -> list[str]:
        request = authorization_pb2.ListPermissionHoldersRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        try:
            answer = await ask(self._clients["authorization"],
                               self._stubs["authorization"]().ListPermissionHolders, request,
                               "Authorization")  # fmt: skip
        except NotFoundError as error:
            raise UnavailableError("Authorization refused to say who holds it") from error
        return list(answer.user_ids)

    async def wind_farm(self, turbine_id: str) -> str:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        try:
            answer = await ask(self._clients["asset"], self._stubs["asset"]().GetTurbine,
                               request, "The Asset Service")  # fmt: skip
        except NotFoundError:
            return ""
        return str(answer.turbine.wind_farm_id)


class AuthorizationClient:
    """Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str) -> bool:
        request = authorization_pb2.CheckRequest(permission=permission)
        try:
            answer = await ask(self._client, self._stub().Check, request, "Authorization")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)
