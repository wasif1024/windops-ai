"""The audit explorer: the tenant's audit trail, its chain checked again, and what notifications
reached whom (M11.6).

It decides nothing. The Audit Service lets only those with audit.read read the trail, in their own
tenant, and nothing can change a record through it; the Notification Service the same for its
deliveries. Each call is made as the signed-in user.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel
from windops.audit.v1 import audit_pb2
from windops.notification.v1 import notification_pb2 as ntf

from windops_web_bff.common import ID_PATTERN, Call, time_of
from windops_web_bff.tokens import Caller

# What a record names: actions such as "knowledge_document.held", and IDs such as "WS-1@1" or
# "kn:WS-1@1#0". The Audit Service matches them as data, and checks their length again.
TEXT = r"^[A-Za-z0-9_.:@#/-]{1,128}$"
OUTCOMES = r"^(succeeded|denied|failed)$"
DELIVERY_STATUSES = r"^(delivered|pending|recorded|failed)$"

Text = Annotated[str, Query(pattern=TEXT)]


class AuditRecord(BaseModel):
    sequence: int
    event_id: str
    occurred_at: str | None
    recorded_at: str | None
    producer: str
    action: str
    actor_type: str
    actor_id: str
    on_behalf_of: str
    resource_type: str
    resource_id: str
    outcome: str
    correlation_id: str
    previous_hash: str
    record_hash: str


class AuditPage(BaseModel):
    records: list[AuditRecord]
    next_page_token: str


class ChainCheck(BaseModel):
    valid: bool
    records_checked: int
    # The first record that does not match, when not valid.
    first_invalid_sequence: int


class DeliveryRecord(BaseModel):
    notification_id: str
    kind: str
    # The recipient's user ID, never their name or address.
    user_id: str
    case_id: str
    # "in_app" or "email".
    channel: str
    # "delivered", "pending", "recorded" or "failed".
    status: str
    provider: str
    attempts: int
    updated_at: str | None


def _record(r: Any) -> AuditRecord:
    return AuditRecord(
        sequence=r.sequence, event_id=r.event_id, occurred_at=time_of(r, "occurred_at"),
        recorded_at=time_of(r, "recorded_at"), producer=r.producer, action=r.action,
        actor_type=r.actor_type, actor_id=r.actor_id, on_behalf_of=r.on_behalf_of,
        resource_type=r.resource_type, resource_id=r.resource_id, outcome=r.outcome,
        correlation_id=r.correlation_id, previous_hash=r.previous_hash,
        record_hash=r.record_hash,
    )  # fmt: skip


def _stamp(value: datetime) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def add_audit_routes(
    router: APIRouter, caller: Callable[..., Awaitable[Caller]], call_as: Call
) -> None:
    Who = Annotated[Caller, Depends(caller)]  # noqa: N806  # A type, named as one.

    @router.get(
        "/audit/records", response_model=AuditPage,
        summary="The tenant's audit trail, newest first by default, narrowed by any filters",
    )  # fmt: skip
    async def records(
        who: Who,
        action: Text = "",
        action_prefix: Text = "",
        actor_id: Text = "",
        resource_type: Text = "",
        resource_id: Text = "",
        outcome: Annotated[str, Query(pattern=OUTCOMES)] = "",
        correlation_id: Text = "",
        producer: Text = "",
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        newest_first: bool = True,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(pattern=r"^\d{0,19}$")] = "",
    ) -> AuditPage:
        request = audit_pb2.ListAuditRecordsRequest(
            page_size=page_size, page_token=page_token, action=action,
            action_prefix=action_prefix, actor_id=actor_id, resource_type=resource_type,
            resource_id=resource_id, outcome=outcome, correlation_id=correlation_id,
            producer=producer, newest_first=newest_first,
            from_time=_stamp(from_time) if from_time else None,
            to_time=_stamp(to_time) if to_time else None,
        )  # fmt: skip
        answer = await call_as("audit", "ListAuditRecords", request, who)
        return AuditPage(records=[_record(r) for r in answer.records],
                         next_page_token=answer.next_page_token)  # fmt: skip

    @router.get(
        "/audit/verify", response_model=ChainCheck,
        summary="Recompute the tenant's hash chain, and say where it first breaks, if anywhere",
    )  # fmt: skip
    async def verify(who: Who) -> ChainCheck:
        answer = await call_as("audit", "VerifyAuditChain", audit_pb2.VerifyAuditChainRequest(),
                               who)  # fmt: skip
        return ChainCheck(valid=answer.valid, records_checked=answer.records_checked,
                          first_invalid_sequence=answer.first_invalid_sequence)  # fmt: skip

    @router.get(
        "/audit/deliveries", response_model=list[DeliveryRecord],
        summary="What notifications reached whom in the tenant, newest first",
    )  # fmt: skip
    async def deliveries(
        who: Who,
        case_id: Annotated[str, Query(pattern=ID_PATTERN)] = "",
        status: Annotated[str, Query(pattern=DELIVERY_STATUSES)] = "",
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[DeliveryRecord]:
        request = ntf.ListDeliveriesRequest(case_id=case_id, status=status, page_size=page_size)
        answer = await call_as("notification", "ListDeliveries", request, who)
        return [
            DeliveryRecord(
                notification_id=d.notification_id, kind=d.kind, user_id=d.user_id,
                case_id=d.case_id, channel=d.delivery.channel, status=d.delivery.status,
                provider=d.delivery.provider, attempts=d.delivery.attempts,
                updated_at=time_of(d.delivery, "updated_at"),
            )
            for d in answer.deliveries
        ]  # fmt: skip
