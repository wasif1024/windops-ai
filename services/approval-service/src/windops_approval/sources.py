"""The owners this service asks, each as the caller, so each decides for them.

Nothing here falls back: what cannot be asked raises UnavailableError, and a decision that
needed it is refused (EVD-010, SRS 7.7).
"""

from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from windops.asset.v1 import asset_pb2
from windops.authorization.v1 import authorization_pb2
from windops.investigation.v1 import investigation_pb2
from windops.maintenance.v1 import maintenance_pb2
from windops_grpc import NotFoundError, ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

DEADLINE_SECONDS = 3.0


async def ask(client: ServiceClient, method: Any, request: Message, who: str) -> Any:
    try:
        return await client.call(
            method, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
        )
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError(f"{who} has no such record for this caller") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...


class Sources(Protocol):
    async def work_order(self, work_order_id: str) -> Any: ...
    async def case(self, case_id: str) -> Any: ...
    async def turbine(self, turbine_id: str) -> Any: ...


class AuthorizationClient:
    """Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        try:
            answer = await ask(self._client, self._stub().Check, request, "Authorization")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)


class ServiceSources:
    def __init__(self, stubs: dict[str, Any], clients: dict[str, ServiceClient]) -> None:
        self._stubs, self._clients = stubs, clients

    async def work_order(self, work_order_id: str) -> Any:
        request = maintenance_pb2.GetWorkOrderRequest(work_order_id=work_order_id)
        answer = await ask(self._clients["maintenance"],
                           self._stubs["maintenance"]().GetWorkOrder, request,
                           "The Maintenance Service")  # fmt: skip
        return answer.work_order

    async def case(self, case_id: str) -> Any:
        request = investigation_pb2.GetCaseRequest(case_id=case_id)
        answer = await ask(self._clients["investigation"],
                           self._stubs["investigation"]().GetCase, request,
                           "The Investigation Service")  # fmt: skip
        return answer.case

    async def turbine(self, turbine_id: str) -> Any:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        answer = await ask(self._clients["asset"], self._stubs["asset"]().GetTurbine, request,
                           "The Asset Service")  # fmt: skip
        return answer.turbine
