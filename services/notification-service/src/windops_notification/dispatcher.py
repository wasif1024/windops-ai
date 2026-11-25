"""Turn each event heard into notifications, and send each email (FR-NOT-001).

**Whom:** the tenant's rules for the event's kind (rules.py), each audience asked of its owner as
this service: who holds a permission, from Authorization, scoped to the turbine's wind farm from
Asset. Each person is told once an event, by every channel any rule gives them. A work order's
drafter and submitter are never asked to approve it (RBAC-004).

**When an owner is down:** the event waits and is tried again, sooner then later, up to every
five minutes, until a day after it happened (SRS 7.7). Nothing is guessed.

**Delivery:** in-app is delivered as it is made, in the same transaction. Email waits for the
sender, so an email outage never holds up the app (SRS 7.7): it is tried five times, then failed.
"""

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from opentelemetry import metrics
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_grpc import DomainError, acting_as_service
from windops_migrations import across_tenants, in_tenant

from windops_notification import rules
from windops_notification.consumer import TOO_OLD
from windops_notification.senders import Email, Sender, SendError
from windops_notification.sources import Directory
from windops_notification.store import (
    CUSTOMIZED,
    DELIVERED,
    DELIVERIES,
    DONE,
    EMAIL,
    FAILED,
    HEARD,
    IN_APP,
    NOTIFICATIONS,
    PENDING,
    RULES,
    SERVICE_NAME,
    SKIPPED,
    WAITING,
    WORK_ORDERS,
    utc,
)

logger = logging.getLogger("windops.service")
EMAIL_ATTEMPTS = 5
LONGEST_WAIT = timedelta(minutes=5)
_meter = metrics.get_meter("windops.notification")
_made = _meter.create_counter("windops_notifications", description="Notifications made, by kind")
_sent = _meter.create_counter(
    "windops_notification_deliveries", description="Deliveries ended, by channel and status"
)


async def rules_for(connection: AsyncConnection, tenant: str) -> tuple[list[rules.Rule], bool]:
    """The tenant's own rules, or the defaults while it has none. (rules, customized)"""
    await in_tenant(connection, tenant)
    customized = await connection.scalar(
        select(CUSTOMIZED.c.tenant_id).where(CUSTOMIZED.c.tenant_id == tenant)
    )
    if customized is None:
        return list(rules.DEFAULTS), False
    r = RULES.c
    rows = (await connection.execute(
        select(RULES).where(r.tenant_id == tenant).order_by(r.kind, r.audience)
    )).all()  # fmt: skip
    return [
        rules.Rule(row.kind, row.audience, tuple(row.channels.split(","))) for row in rows
    ], True


def _wait(attempts: int) -> timedelta:
    return min(timedelta(seconds=5 * 2**attempts), LONGEST_WAIT)


@dataclass
class Dispatcher:
    engine: AsyncEngine
    directory: Directory
    sender: Sender
    secret: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def run_once(self) -> int:
        """Work out who to tell for what is waiting, then send what is due. How many changed."""
        now = self.clock()
        async with self.engine.connect() as connection:
            await across_tenants(connection)
            h, d = HEARD.c, DELIVERIES.c
            waiting = (await connection.execute(
                select(h.tenant_id, h.event_id).where(h.status == WAITING,
                                                      h.next_attempt_at <= now)
                .order_by(h.occurred_at).limit(100)
            )).all()  # fmt: skip
            due = (await connection.execute(
                select(d.tenant_id, d.notification_id).where(
                    d.channel == EMAIL, d.status == PENDING, d.next_attempt_at <= now)
                .order_by(d.created_at).limit(100)
            )).all()  # fmt: skip
        changed = 0
        for tenant, event_id in waiting:
            changed += await self.notify(tenant, event_id)
        for tenant, notification_id in due:
            changed += await self.send(tenant, notification_id)
        return changed

    async def notify(self, tenant: str, event_id: str) -> int:
        async with self.engine.connect() as connection:
            await in_tenant(connection, tenant)
            h = HEARD.c
            heard = (await connection.execute(select(HEARD).where(
                h.tenant_id == tenant, h.event_id == event_id))).one_or_none()  # fmt: skip
            if heard is None or heard.status != WAITING:
                return 0
            chosen, _ = await rules_for(connection, tenant)
            facts: dict[str, str] = json.loads(heard.facts)
            if heard.kind == rules.DECISION_MADE:
                facts.update(await self._work_order(connection, tenant, facts["work_order_id"]))
        now = self.clock()
        if now - utc(heard.occurred_at) > TOO_OLD:
            await self._settle(tenant, event_id, SKIPPED, "Nobody could be told within a day")
            return 1
        try:
            with acting_as_service(SERVICE_NAME, tenant, self.secret):
                channels = await self._recipients(heard.kind, facts, chosen)
        except DomainError as error:
            await self._later(tenant, event_id, heard.attempts, f"{type(error).__name__}: {error}")
            return 0
        message = rules.message(heard.kind, facts)
        async with self.engine.begin() as connection:
            await in_tenant(connection, tenant)
            mine = (h.tenant_id == tenant, h.event_id == event_id)
            locked = (await connection.execute(
                select(h.status).where(*mine).with_for_update()
            )).scalar()  # fmt: skip
            if locked != WAITING:
                return 0
            for user, ways in sorted(channels.items()):
                notification_id = f"ntf_{secrets.token_hex(10)}"
                await connection.execute(insert(NOTIFICATIONS).values(
                    tenant_id=tenant, notification_id=notification_id, user_id=user,
                    kind=heard.kind, title=message.title, body=message.body, link=message.link,
                    case_id=facts.get("case_id", ""), work_order_id=facts.get("work_order_id", ""),
                    event_id=event_id, created_at=now,
                ))  # fmt: skip
                for channel in sorted(ways):
                    in_app = channel == IN_APP
                    await connection.execute(insert(DELIVERIES).values(
                        tenant_id=tenant, notification_id=notification_id, channel=channel,
                        status=DELIVERED if in_app else PENDING,
                        provider=IN_APP if in_app else "", attempts=1 if in_app else 0,
                        next_attempt_at=now, created_at=now, updated_at=now,
                    ))  # fmt: skip
                    if in_app:
                        _sent.add(1, {"channel": IN_APP, "status": DELIVERED})
                _made.add(1, {"kind": heard.kind})
            await connection.execute(update(HEARD).where(
                h.tenant_id == tenant, h.event_id == event_id
            ).values(status=DONE, note=f"{len(channels)} told", updated_at=now))  # fmt: skip
        return 1

    async def _work_order(self, connection: AsyncConnection, tenant: str,
                          work_order_id: str) -> dict[str, str]:  # fmt: skip
        w = WORK_ORDERS.c
        row = (await connection.execute(select(WORK_ORDERS).where(
            w.tenant_id == tenant, w.work_order_id == work_order_id))).one_or_none()  # fmt: skip
        if row is None:
            return {}
        return {"turbine_id": row.turbine_id, "drafted_by": row.drafted_by,
                "submitted_by": row.submitted_by}  # fmt: skip

    async def _recipients(self, kind: str, facts: dict[str, str],
                          chosen: list[rules.Rule]) -> dict[str, set[str]]:  # fmt: skip
        """Each person to tell, and by which channels."""
        turbine = facts.get("turbine_id", "")
        farm: str | None = None
        told: dict[str, set[str]] = {}
        for rule in chosen:
            if rule.kind != kind:
                continue
            audience = rule.audience
            if audience == rules.REQUESTER and not rules.is_person(facts.get("requested_by", "")):
                # A case from detections: everyone who may review its findings there.
                audience = f"{rules.HOLDERS}{rules.REVIEWERS}"
            if audience.startswith(rules.HOLDERS):
                if farm is None:
                    farm = await self.directory.wind_farm(turbine) if turbine else ""
                people = await self.directory.holders(
                    audience.removeprefix(rules.HOLDERS), farm, turbine if farm else ""
                )
            else:
                field = {rules.REQUESTER: "requested_by", rules.DRAFTER: "drafted_by",
                         rules.SUBMITTER: "submitted_by"}[audience]  # fmt: skip
                people = [facts.get(field, "")]
            for person in people:
                if rules.is_person(person):
                    told.setdefault(person, set()).update(rule.channels)
        if kind == rules.APPROVAL_REQUESTED:
            # Never asked to approve what they drafted or submitted (RBAC-004).
            for own in (facts.get("drafted_by", ""), facts.get("submitted_by", "")):
                told.pop(own, None)
        return told

    async def _later(self, tenant: str, event_id: str, attempts: int, why: str) -> None:
        logger.warning("Could not work out whom to tell yet", extra={"event_id": event_id})
        now = self.clock()
        async with self.engine.begin() as connection:
            await in_tenant(connection, tenant)
            h = HEARD.c
            await connection.execute(update(HEARD).where(
                h.tenant_id == tenant, h.event_id == event_id, h.status == WAITING
            ).values(attempts=attempts + 1, next_attempt_at=now + _wait(attempts),
                     note=why[:500], updated_at=now))  # fmt: skip

    async def _settle(self, tenant: str, event_id: str, status: str, why: str) -> None:
        async with self.engine.begin() as connection:
            await in_tenant(connection, tenant)
            h = HEARD.c
            await connection.execute(update(HEARD).where(
                h.tenant_id == tenant, h.event_id == event_id, h.status == WAITING
            ).values(status=status, note=why, updated_at=self.clock()))  # fmt: skip

    async def send(self, tenant: str, notification_id: str) -> int:
        async with self.engine.connect() as connection:
            await in_tenant(connection, tenant)
            n, d = NOTIFICATIONS.c, DELIVERIES.c
            found = (await connection.execute(
                select(NOTIFICATIONS, d.attempts).join(
                    DELIVERIES, (d.tenant_id == n.tenant_id)
                    & (d.notification_id == n.notification_id) & (d.channel == EMAIL))
                .where(n.tenant_id == tenant, n.notification_id == notification_id,
                       d.status == PENDING)
            )).one_or_none()  # fmt: skip
        if found is None:
            return 0
        email = Email(tenant_id=tenant, user_id=found.user_id, subject=found.title,
                      text=f"{found.body}\n\nOpen it: {found.link}")  # fmt: skip
        attempts, error = found.attempts + 1, ""
        try:
            status = await self.sender.send(email)
        except SendError as failure:
            error = str(failure)[:500]
            status = FAILED if attempts >= EMAIL_ATTEMPTS else PENDING
        now = self.clock()
        async with self.engine.begin() as connection:
            await in_tenant(connection, tenant)
            await connection.execute(update(DELIVERIES).where(
                d.tenant_id == tenant, d.notification_id == notification_id, d.channel == EMAIL,
                d.status == PENDING,
            ).values(status=status, provider=self.sender.name, attempts=attempts,
                     last_error=error, next_attempt_at=now + _wait(attempts),
                     updated_at=now))  # fmt: skip
        if status != PENDING:
            _sent.add(1, {"channel": EMAIL, "status": status})
        return 1


async def keep_dispatching(dispatcher: Dispatcher, stop: asyncio.Event,
                           every_seconds: float = 2.0) -> None:  # fmt: skip
    while not stop.is_set():
        try:
            await dispatcher.run_once()
        except Exception as error:  # Tried again shortly. What waits is safe meanwhile.
            logger.warning("Could not dispatch notifications",
                           extra={"error_type": type(error).__name__})  # fmt: skip
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)
