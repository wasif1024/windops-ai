"""The gRPC API: a person's own notifications, the tenant's deliveries, and its rules."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.notification.v1 import notification_pb2 as pb
from windops.notification.v1 import notification_pb2_grpc
from windops_grpc import InvalidRequestError, PermissionDeniedError, current_identity
from windops_migrations import in_tenant

from windops_notification import rules
from windops_notification.dispatcher import rules_for
from windops_notification.sources import Authorizer
from windops_notification.store import (
    CUSTOMIZED,
    DELIVERIES,
    DELIVERY_STATUSES,
    NOTIFICATIONS,
    RULE_CHANGES,
    RULES,
    audit,
)

DEFAULT_PAGE, MAXIMUM_PAGE, MAXIMUM_IDS, MAXIMUM_RULES = 50, 200, 200, 100
AUDIT_READ, USERS_MANAGE = "audit.read", "users.manage"


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def _page(size: int) -> int:
    if size < 0:
        raise InvalidRequestError("page_size cannot be negative")
    return min(size or DEFAULT_PAGE, MAXIMUM_PAGE)


def _delivery(row: Any) -> pb.Delivery:
    return pb.Delivery(
        channel=row.channel, status=row.status, provider=row.provider, attempts=row.attempts,
        updated_at=_timestamp(row.updated_at), last_error=row.last_error,
    )  # fmt: skip


def _rule(rule: rules.Rule) -> pb.Rule:
    return pb.Rule(kind=rule.kind, audience=rule.audience, channels=list(rule.channels))


class NotificationService(notification_pb2_grpc.NotificationServiceServicer):
    def __init__(self, engine: AsyncEngine, authorizer: Authorizer,
                 clock: Callable[[], datetime] | None = None) -> None:  # fmt: skip
        self._engine, self._authorizer = engine, authorizer
        self._now = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _person() -> tuple[str, str]:
        """(tenant, user). Notifications are a person's: an agent or a service has none."""
        identity = current_identity()
        if identity.actor or not identity.user_id:
            raise PermissionDeniedError("Only a person has notifications")
        return identity.tenant_id, identity.user_id

    async def _require(self, permission: str) -> tuple[str, str]:
        tenant, user = self._person()
        if not await self._authorizer.allowed(permission):
            raise PermissionDeniedError(f"You need {permission} for this")
        return tenant, user

    async def ListMyNotifications(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListMyNotificationsRequest, context: Any
    ) -> pb.ListMyNotificationsResponse:
        tenant, user = self._person()
        n, d = NOTIFICATIONS.c, DELIVERIES.c
        mine = (n.tenant_id == tenant, n.user_id == user)
        query = select(NOTIFICATIONS).where(*mine)
        if request.unread_only:
            query = query.where(n.read_at.is_(None))
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(
                query.order_by(n.created_at.desc(), n.notification_id)
                .limit(_page(request.page_size))
            )).all()  # fmt: skip
            unread = (await connection.execute(
                select(func.count()).where(*mine, n.read_at.is_(None))
            )).scalar_one()  # fmt: skip
            ids = [row.notification_id for row in rows]
            deliveries = (await connection.execute(
                select(DELIVERIES).where(d.tenant_id == tenant, d.notification_id.in_(ids))
                .order_by(d.channel)
            )).all() if ids else []  # fmt: skip
        by_id: dict[str, list[pb.Delivery]] = {}
        for row in deliveries:
            by_id.setdefault(row.notification_id, []).append(_delivery(row))
        return pb.ListMyNotificationsResponse(
            notifications=[
                pb.Notification(
                    notification_id=row.notification_id,
                    kind=row.kind,
                    title=row.title,
                    body=row.body,
                    link=row.link,
                    case_id=row.case_id,
                    work_order_id=row.work_order_id,
                    created_at=_timestamp(row.created_at),
                    read_at=_timestamp(row.read_at),
                    deliveries=by_id.get(row.notification_id),
                )
                for row in rows
            ],
            unread_count=unread,
        )

    async def MarkNotificationsRead(  # noqa: N802  # The name comes from the contract.
        self, request: pb.MarkNotificationsReadRequest, context: Any
    ) -> pb.MarkNotificationsReadResponse:
        tenant, user = self._person()
        if len(request.notification_ids) > MAXIMUM_IDS:
            raise InvalidRequestError(f"At most {MAXIMUM_IDS} notifications at once")
        n = NOTIFICATIONS.c
        query = update(NOTIFICATIONS).where(n.tenant_id == tenant, n.user_id == user,
                                            n.read_at.is_(None))  # fmt: skip
        if request.notification_ids:
            query = query.where(n.notification_id.in_(list(request.notification_ids)))
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            marked = (await connection.execute(query.values(read_at=self._now()))).rowcount
        return pb.MarkNotificationsReadResponse(marked=marked)

    async def ListDeliveries(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListDeliveriesRequest, context: Any
    ) -> pb.ListDeliveriesResponse:
        tenant, _ = await self._require(AUDIT_READ)
        if request.status and request.status not in DELIVERY_STATUSES:
            raise InvalidRequestError(f"status must be one of: {', '.join(DELIVERY_STATUSES)}")
        n, d = NOTIFICATIONS.c, DELIVERIES.c
        query = (
            select(DELIVERIES, n.kind, n.user_id, n.case_id)
            .join(NOTIFICATIONS, (n.tenant_id == d.tenant_id)
                  & (n.notification_id == d.notification_id))
            .where(d.tenant_id == tenant)
        )  # fmt: skip
        if request.case_id:
            query = query.where(n.case_id == request.case_id)
        if request.status:
            query = query.where(d.status == request.status)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(
                query.order_by(d.created_at.desc(), d.notification_id, d.channel)
                .limit(_page(request.page_size))
            )).all()  # fmt: skip
        return pb.ListDeliveriesResponse(deliveries=[
            pb.DeliveryRecord(notification_id=row.notification_id, kind=row.kind,
                              user_id=row.user_id, case_id=row.case_id, delivery=_delivery(row))
            for row in rows
        ])  # fmt: skip

    async def GetNotificationRules(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetNotificationRulesRequest, context: Any
    ) -> pb.GetNotificationRulesResponse:
        tenant, _ = await self._require(USERS_MANAGE)
        async with self._engine.connect() as connection:
            chosen, customized = await rules_for(connection, tenant)
        return pb.GetNotificationRulesResponse(rules=[_rule(r) for r in chosen],
                                               customized=customized)  # fmt: skip

    async def SetNotificationRules(  # noqa: N802  # The name comes from the contract.
        self, request: pb.SetNotificationRulesRequest, context: Any
    ) -> pb.SetNotificationRulesResponse:
        tenant, user = await self._require(USERS_MANAGE)
        if len(request.rules) > MAXIMUM_RULES:
            raise InvalidRequestError(f"At most {MAXIMUM_RULES} rules")
        try:
            given = [rules.check(r.kind, r.audience, r.channels) for r in request.rules]
        except rules.InvalidRuleError as error:
            raise InvalidRequestError(str(error)) from error
        if len({(r.kind, r.audience) for r in given}) != len(given):
            raise InvalidRequestError("Each kind and audience at most once")
        if not 8 <= len(request.idempotency_key) <= 128:
            raise InvalidRequestError("idempotency_key must be 8 to 128 characters")
        digest = hashlib.sha256(request.SerializeToString(deterministic=True)).hexdigest()
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            c = RULE_CHANGES.c
            earlier = (await connection.execute(select(c.request_hash).where(
                c.tenant_id == tenant, c.idempotency_key == request.idempotency_key
            ))).scalar()  # fmt: skip
            if earlier is not None:
                if earlier != digest:
                    raise InvalidRequestError("This idempotency key was used for other rules")
                chosen, customized = await rules_for(connection, tenant)
                return pb.SetNotificationRulesResponse(rules=[_rule(r) for r in chosen],
                                                       customized=customized)  # fmt: skip
            await connection.execute(insert(RULE_CHANGES).values(
                tenant_id=tenant, idempotency_key=request.idempotency_key, request_hash=digest,
                set_by=user, set_at=now,
            ))  # fmt: skip
            await connection.execute(delete(RULES).where(RULES.c.tenant_id == tenant))
            await connection.execute(delete(CUSTOMIZED).where(CUSTOMIZED.c.tenant_id == tenant))
            if given:
                await connection.execute(insert(CUSTOMIZED).values(
                    tenant_id=tenant, set_by=user, set_at=now))  # fmt: skip
                for rule in given:
                    await connection.execute(insert(RULES).values(
                        tenant_id=tenant, kind=rule.kind, audience=rule.audience,
                        channels=",".join(rule.channels), set_by=user, set_at=now,
                    ))  # fmt: skip
            await audit(connection, tenant_id=tenant, actor_id=user,
                        action="notification.rules_set", resource_type="notification_rules",
                        resource_id=tenant)  # fmt: skip
            chosen, customized = await rules_for(connection, tenant)
        return pb.SetNotificationRulesResponse(rules=[_rule(r) for r in chosen],
                                               customized=customized)  # fmt: skip
