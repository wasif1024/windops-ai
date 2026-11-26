"""The signed-in person's own notifications, and marking them read (M10.5, FR-NOT-001).

The notification center (M11.6) and the badge in the shell read these. A Tenant Administrator
with users.manage also sets the tenant's rules there: whom each kind goes to, and how.
Each call is made as the signed-in user, so they only ever see their own. A notification holds
identifiers and a link, never evidence (FR-NOT-002).
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from windops.notification.v1 import notification_pb2 as ntf

from windops_web_bff.common import Call, Key, time_of
from windops_web_bff.tokens import Caller


class Delivery(BaseModel):
    # "in_app" or "email".
    channel: str
    # "delivered", "pending", "recorded" or "failed".
    status: str
    attempts: int
    updated_at: str | None


class Notification(BaseModel):
    notification_id: str
    kind: str
    title: str
    body: str
    # A path in the console.
    link: str
    case_id: str
    work_order_id: str
    created_at: str | None
    read_at: str | None
    deliveries: list[Delivery]


class NotificationList(BaseModel):
    notifications: list[Notification]
    unread_count: int


class ReadIn(BaseModel):
    # Empty marks all of them read.
    notification_ids: list[str] = Field(default_factory=list, max_length=200)


class ReadOut(BaseModel):
    marked: int


KINDS = (r"^(approval_requested|decision_made|case_pending_review|case_approved|case_rejected|"
         r"case_needs_attention|case_verified)$")  # fmt: skip
AUDIENCES = r"^(requester|drafter|submitter|holders:[a-z_]{1,40}\.[a-z_]{1,40})$"


class Rule(BaseModel):
    kind: str = Field(pattern=KINDS)
    # "requester", "drafter", "submitter", or "holders:<permission>".
    audience: str = Field(pattern=AUDIENCES)
    # "in_app", "email", or both.
    channels: list[Literal["in_app", "email"]] = Field(min_length=1, max_length=2)


class Rules(BaseModel):
    rules: list[Rule]
    # False while the tenant uses the defaults.
    customized: bool


class RulesIn(BaseModel):
    # Empty goes back to the defaults.
    rules: list[Rule] = Field(default_factory=list, max_length=100)


def _rules(answer: Any) -> Rules:
    return Rules(
        rules=[Rule(kind=r.kind, audience=r.audience, channels=list(r.channels))
               for r in answer.rules],
        customized=answer.customized,
    )  # fmt: skip


def _notification(message: Any) -> Notification:
    return Notification(
        notification_id=message.notification_id, kind=message.kind, title=message.title,
        body=message.body, link=message.link, case_id=message.case_id,
        work_order_id=message.work_order_id, created_at=time_of(message, "created_at"),
        read_at=time_of(message, "read_at"),
        deliveries=[
            Delivery(channel=d.channel, status=d.status, attempts=d.attempts,
                     updated_at=time_of(d, "updated_at"))
            for d in message.deliveries
        ],
    )  # fmt: skip


def add_notification_routes(
    router: APIRouter, caller: Callable[..., Awaitable[Caller]], call_as: Call
) -> None:
    @router.get(
        "/notifications", response_model=NotificationList,
        summary="The user's own notifications, newest first, with each one's delivery status",
    )  # fmt: skip
    async def list_notifications(
        who: Annotated[Caller, Depends(caller)],
        unread_only: bool = False,
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> NotificationList:
        answer = await call_as(
            "notification", "ListMyNotifications",
            ntf.ListMyNotificationsRequest(unread_only=unread_only, page_size=page_size), who,
        )  # fmt: skip
        return NotificationList(
            notifications=[_notification(n) for n in answer.notifications],
            unread_count=answer.unread_count,
        )

    @router.post(
        "/notifications/read", response_model=ReadOut,
        summary="Mark the user's own notifications read: the ones named, or all of them",
    )  # fmt: skip
    async def mark_read(body: ReadIn, who: Annotated[Caller, Depends(caller)]) -> ReadOut:
        answer = await call_as(
            "notification", "MarkNotificationsRead",
            ntf.MarkNotificationsReadRequest(notification_ids=body.notification_ids), who,
        )  # fmt: skip
        return ReadOut(marked=answer.marked)

    @router.get(
        "/notifications/rules", response_model=Rules,
        summary="The tenant's rules: whom each kind of notification goes to, and how",
    )  # fmt: skip
    async def get_rules(who: Annotated[Caller, Depends(caller)]) -> Rules:
        answer = await call_as("notification", "GetNotificationRules",
                               ntf.GetNotificationRulesRequest(), who)  # fmt: skip
        return _rules(answer)

    @router.put(
        "/notifications/rules", response_model=Rules,
        summary="Replace the tenant's rules, or go back to the defaults with none",
    )  # fmt: skip
    async def set_rules(body: RulesIn, key: Key, who: Annotated[Caller, Depends(caller)]) -> Rules:
        request = ntf.SetNotificationRulesRequest(
            rules=[ntf.Rule(kind=r.kind, audience=r.audience, channels=r.channels)
                   for r in body.rules],
            idempotency_key=key,
        )  # fmt: skip
        answer = await call_as("notification", "SetNotificationRules", request, who, retry=False)
        return _rules(answer)
