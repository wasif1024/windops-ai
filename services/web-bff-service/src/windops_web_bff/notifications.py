"""The signed-in person's own notifications, and marking them read (M10.5, FR-NOT-001).

The notification center screen comes in M11.6; this is what it, and a badge in the shell, read.
Each call is made as the signed-in user, so they only ever see their own. A notification holds
identifiers and a link, never evidence (FR-NOT-002).
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from windops.notification.v1 import notification_pb2 as ntf

from windops_web_bff.common import Call, time_of
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
