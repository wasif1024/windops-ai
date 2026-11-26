"""The Authorization Service, asked as the caller. Any failure denies (SRS 7.7)."""

from typing import Any

import grpc
from windops.authorization.v1 import authorization_pb2
from windops_grpc import ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

DEADLINE_SECONDS = 3.0


class AuthorizationClient:
    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str) -> bool:
        request = authorization_pb2.CheckRequest(permission=permission)
        try:
            answer = await self._client.call(
                self._stub().Check, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)
