"""A tenant's egress switches, for its administrators' console (EGR-002, ADR 0048).

It decides nothing: the Tenant Service needs mcp.configure, which SRS 3.2 gives to Tenant
Administrators, a person and a recent second factor to turn a path on or off, and a reason.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from windops.tenant.v1 import tenant_pb2

from windops_web_bff.common import Call, Key, time_of
from windops_web_bff.tokens import Caller


class EgressPath(BaseModel):
    # "claude", "embeddings", "langsmith_traces", "langsmith_sandbox" or "forecasts".
    path: str
    enabled: bool
    # Where it goes and what goes there.
    destination: str
    # What stops while it is off.
    when_off: str
    # Why it was last changed, by whom and when. Empty while it has never been changed.
    reason: str
    updated_by: str
    updated_at: str | None


class EgressPaths(BaseModel):
    paths: list[EgressPath]


class EgressChange(BaseModel):
    enabled: bool
    reason: str = Field(min_length=10, max_length=500)


def _path(message: Any) -> EgressPath:
    return EgressPath(
        path=message.path,
        enabled=message.enabled,
        destination=message.destination,
        when_off=message.when_off,
        reason=message.reason,
        updated_by=message.updated_by,
        updated_at=time_of(message, "updated_at"),
    )


def add_egress_routes(router: APIRouter, caller: Any, call: Call) -> None:
    """Add the routes to the versioned router."""
    Who = Annotated[Caller, Depends(caller)]  # noqa: N806  # A type, named as one.

    @router.get(
        "/admin/egress",
        response_model=EgressPaths,
        summary="Each path out of the platform, whether it is on, and what stops while it is off",
    )
    async def get_egress(who: Who) -> EgressPaths:
        answer = await call("tenant", "GetEgress", tenant_pb2.GetEgressRequest(), who)
        return EgressPaths(paths=[_path(item) for item in answer.paths])

    @router.put(
        "/admin/egress/{path}",
        response_model=EgressPath,
        summary="Turn a path out of the platform on or off, with a reason",
    )
    async def set_egress(
        who: Who,
        path: Annotated[str, Path(pattern=r"^[a-z_]{1,40}$")],
        body: EgressChange,
        key: Key,
    ) -> EgressPath:
        request = tenant_pb2.SetEgressRequest(
            path=path, enabled=body.enabled, reason=body.reason, idempotency_key=key
        )
        answer = await call("tenant", "SetEgress", request, who, retry=False)
        return _path(answer.path)
