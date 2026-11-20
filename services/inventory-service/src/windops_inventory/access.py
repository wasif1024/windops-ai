"""What the caller may do, asked of the Authorization Service as the caller. Any failure denies
(SRS 7.7)."""

from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from windops.authorization.v1 import authorization_pb2
from windops_grpc import ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

DEADLINE_SECONDS = 3.0


class Authorizer(Protocol):
    async def allowed(self, permission: str, wind_farm_id: str = "") -> bool: ...

    async def anywhere(self, permission: str) -> bool:
        """Whether the caller holds it in any scope in their tenant."""
        ...


class AuthorizationClient:
    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def _ask(self, method: Any, request: Message) -> Any:
        try:
            return await self._client.call(
                method, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("Permissions cannot be checked right now") from error

    async def allowed(self, permission: str, wind_farm_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission, scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id)
        )
        return bool((await self._ask(self._stub().Check, request)).allowed)

    async def anywhere(self, permission: str) -> bool:
        if await self.allowed(permission):
            return True
        # ListMyPermissions decides nothing on its own, so a scoped grant it names is then
        # confirmed by Check, for that scope.
        listed = await self._ask(
            self._stub().ListMyPermissions, authorization_pb2.ListMyPermissionsRequest()
        )
        for held in listed.permissions:
            if held.permission == permission:
                farm = held.scope.wind_farm_id
                if farm and await self.allowed(permission, farm):
                    return True
                if held.scope.turbine_id:
                    check = authorization_pb2.CheckRequest(
                        permission=permission,
                        scope=authorization_pb2.Scope(
                            wind_farm_id=farm, turbine_id=held.scope.turbine_id
                        ),
                    )
                    if (await self._ask(self._stub().Check, check)).allowed:
                        return True
        return False
