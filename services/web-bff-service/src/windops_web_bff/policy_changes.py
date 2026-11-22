"""Changes to who may approve maintenance, waiting for a second person (RBAC-007, M10.2b).

It decides nothing: the Authorization Service holds the change, refuses its proposer as the one
who confirms it, and lets a Platform Administrator confirm only where the tenant has no other
Tenant Administrator. Each call is made as the signed-in user, and never retried here.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from windops.authorization.v1 import authorization_pb2 as pb

from windops_web_bff.common import TENANT_PATTERN, Call, Key, time_of
from windops_web_bff.tokens import Caller

ChangeId = Annotated[str, Path(pattern=r"^[0-9a-f-]{36}$")]
TenantId = Annotated[str, Path(pattern=TENANT_PATTERN)]


class PendingChange(BaseModel):
    change_id: str
    tenant_id: str
    # "grant", "revoke" or "role_permissions".
    kind: str
    summary: str
    proposed_by: str
    proposed_at: str | None
    expires_at: str | None
    # "pending", "confirmed", "declined" or "expired".
    status: str
    decided_by: str
    decided_at: str | None
    decline_reason: str
    # No other Tenant Administrator could confirm it, so a Platform Administrator may.
    escalated: bool
    role: str
    grantee_id: str
    permissions: list[str]


class PendingChanges(BaseModel):
    changes: list[PendingChange]


class Decline(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def pending_change(message: Any) -> PendingChange:
    return PendingChange(
        change_id=message.change_id, tenant_id=message.tenant_id, kind=message.kind,
        summary=message.summary, proposed_by=message.proposed_by,
        proposed_at=time_of(message, "proposed_at"), expires_at=time_of(message, "expires_at"),
        status=message.status, decided_by=message.decided_by,
        decided_at=time_of(message, "decided_at"), decline_reason=message.decline_reason,
        escalated=message.escalated, role=message.role, grantee_id=message.grantee_id,
        permissions=list(message.permissions),
    )  # fmt: skip


def add_policy_change_routes(router: APIRouter, caller: Any, call: Call) -> None:
    Who = Annotated[Caller, Depends(caller)]  # noqa: N806  # A type, named as one.

    async def listed(who: Caller, tenant: str = "") -> PendingChanges:
        request = pb.ListPendingChangesRequest(target_tenant_id=tenant, include_decided=True)
        answer = await call("authorization", "ListPendingChanges", request, who)
        return PendingChanges(changes=[pending_change(c) for c in answer.changes])

    async def confirm(who: Caller, change_id: str, key: str, tenant: str = "") -> PendingChange:
        request = pb.ConfirmChangeRequest(target_tenant_id=tenant, change_id=change_id,
                                          idempotency_key=key)  # fmt: skip
        answer = await call("authorization", "ConfirmChange", request, who, retry=False)
        return pending_change(answer.change)

    async def decline(who: Caller, change_id: str, body: Decline, key: str,
                      tenant: str = "") -> PendingChange:  # fmt: skip
        request = pb.DeclineChangeRequest(target_tenant_id=tenant, change_id=change_id,
                                          reason=body.reason, idempotency_key=key)  # fmt: skip
        answer = await call("authorization", "DeclineChange", request, who, retry=False)
        return pending_change(answer.change)

    # --- A Tenant Administrator's own tenant

    @router.get(
        "/admin/policy-changes", response_model=PendingChanges,
        summary="Changes to who may approve maintenance, newest first, pending or decided",
    )  # fmt: skip
    async def tenant_changes(who: Who) -> PendingChanges:
        return await listed(who)

    @router.post(
        "/admin/policy-changes/{change_id}/confirm", response_model=PendingChange,
        summary="Confirm another administrator's change. Never one's own",
    )  # fmt: skip
    async def tenant_confirm(who: Who, change_id: ChangeId, key: Key) -> PendingChange:
        return await confirm(who, change_id, key)

    @router.post(
        "/admin/policy-changes/{change_id}/decline", response_model=PendingChange,
        summary="Withdraw one's own change, or refuse another's, with a reason",
    )  # fmt: skip
    async def tenant_decline(who: Who, change_id: ChangeId, body: Decline,
                             key: Key) -> PendingChange:  # fmt: skip
        return await decline(who, change_id, body, key)

    # --- A Platform Administrator's escalations

    @router.get(
        "/platform/policy-changes", response_model=PendingChanges,
        summary="Changes escalated from tenants with no second Tenant Administrator",
    )  # fmt: skip
    async def escalations(who: Who) -> PendingChanges:
        return await listed(who)

    @router.post(
        "/platform/tenants/{tenant_id}/policy-changes/{change_id}/confirm",
        response_model=PendingChange,
        summary="Confirm an escalated change in a tenant with no second Tenant Administrator",
    )
    async def platform_confirm(who: Who, tenant_id: TenantId, change_id: ChangeId,
                               key: Key) -> PendingChange:  # fmt: skip
        return await confirm(who, change_id, key, tenant_id)

    @router.post(
        "/platform/tenants/{tenant_id}/policy-changes/{change_id}/decline",
        response_model=PendingChange,
        summary="Refuse an escalated change, with a reason",
    )
    async def platform_decline(who: Who, tenant_id: TenantId, change_id: ChangeId,
                               body: Decline, key: Key) -> PendingChange:  # fmt: skip
        return await decline(who, change_id, body, key, tenant_id)
