"""Notifications from events, whom they reach, their delivery, and the API (M10.5), on SQLite
with stand-ins for Authorization and Asset."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.events.v1 import events_pb2
from windops.notification.v1 import notification_pb2 as pb
from windops.notification.v1 import notification_pb2_grpc
from windops_events import Event, InvalidEventError, parse_event
from windops_grpc import (
    ServiceRuntime,
    Settings,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_notification import consumer, rules
from windops_notification.dispatcher import Dispatcher
from windops_notification.senders import Email, Recording, SendError
from windops_notification.service import NotificationService
from windops_notification.store import DELIVERIES, HEARD, METADATA, NOTIFICATIONS, SERVICE_NAME
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(environment="test", version="test", identity_secret=SECRET, grpc_port=0,
                    http_port=0, shutdown_grace_seconds=1)  # fmt: skip
NOW = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
WORK_ORDER, CASE, TURBINE = "wo_" + "a" * 20, "case_" + "b" * 20, "dogger-east-t01"
PLANNER, MANAGER, FARM_MANAGER, BOTH = "u-planner", "u-manager", "u-farm-manager", "u-both"
ENGINEER, ADMIN, AUDITOR = "u-engineer", "u-admin", "u-auditor"
PRINCIPAL = "service:fault-investigator"


@dataclass
class FakeDirectory:
    """Holders by (permission, wind farm). A farm grant covers a turbine there only."""

    holders_of: dict[tuple[str, str], list[str]] = field(default_factory=lambda: {
        ("maintenance.approve", ""): [MANAGER, BOTH],
        ("maintenance.approve", "dogger-east"): [FARM_MANAGER, MANAGER, BOTH],
        ("finding.review", "dogger-east"): [ENGINEER],
    })  # fmt: skip
    down: bool = False
    asked: list[tuple[str, str, str, str]] = field(default_factory=list)

    async def holders(self, permission: str, wind_farm_id: str, turbine_id: str) -> list[str]:
        self.asked.append((current_identity().subject, permission, wind_farm_id, turbine_id))
        if self.down:
            raise UnavailableError("Authorization cannot be reached right now")
        return self.holders_of.get((permission, wind_farm_id), [])

    async def wind_farm(self, turbine_id: str) -> str:
        if self.down:
            raise UnavailableError("The Asset Service cannot be reached right now")
        return "dogger-east" if turbine_id == TURBINE else ""


@dataclass
class FakeAuthorizer:
    grants: set[tuple[str, str]] = field(default_factory=lambda: {
        (ADMIN, "users.manage"), (AUDITOR, "audit.read"),
    })  # fmt: skip

    async def allowed(self, permission: str) -> bool:
        return (str(current_identity().user_id), permission) in self.grants


class Failing(Recording):
    async def send(self, email: Email) -> str:
        raise SendError("The provider is down")


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    directory: FakeDirectory
    dispatcher: Dispatcher
    clock: list[datetime]

    async def hear(
        self,
        subject: str,
        payload: Any,
        event_id: str = "e-1",
        at: datetime = NOW,
        producer: str = "",
        tenant: str = TENANT,
    ) -> bool:
        event = Event(subject, event_id, tenant, at, producer or consumer.PRODUCERS[subject],
                      payload, "", "")  # fmt: skip
        return await consumer.hear(self.engine, subject, event, now=self.clock[0])

    async def told(self) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(NOTIFICATIONS))).all()
        return {row.user_id: row for row in rows}

    async def deliveries(self) -> dict[tuple[str, str], Any]:
        async with self.engine.connect() as connection:
            n, d = NOTIFICATIONS.c, DELIVERIES.c
            rows = (await connection.execute(
                select(DELIVERIES, n.user_id).join(
                    NOTIFICATIONS, n.notification_id == d.notification_id)
            )).all()  # fmt: skip
        return {(row.user_id, row.channel): row for row in rows}


def as_caller(who: str, tenant: str = TENANT, actor: str | None = None,
              person: bool = True) -> Any:  # fmt: skip
    identity = VerifiedIdentity(subject=who, tenant_id=tenant, user_id=who if person else None,
                                auth_time=int(time.time()), actor=actor)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        return error.code()
    raise AssertionError("The call succeeded")


def submitted(version: str = "1", drafted_by: str = PLANNER,
              submitted_by: str = PLANNER) -> events_pb2.WorkOrderSubmitted:  # fmt: skip
    return events_pb2.WorkOrderSubmitted(
        work_order_id=WORK_ORDER, proposal_version=version, case_id=CASE, turbine_id=TURBINE,
        evidence_snapshot_id="evs_" + "c" * 20, submitted_by=submitted_by, drafted_by=drafted_by,
    )  # fmt: skip


def decided(decision: int = events_pb2.APPROVAL_DECISION_APPROVED) -> events_pb2.ApprovalDecided:
    return events_pb2.ApprovalDecided(
        approval_id="apr_" + "1" * 20, work_order_id=WORK_ORDER, proposal_version="1",
        evidence_snapshot_id="evs_" + "c" * 20, decision=decision,  # type: ignore[arg-type]
        decided_by=MANAGER, case_id=CASE,
    )  # fmt: skip


def case_updated(action: str, requested_by: str = ENGINEER) -> events_pb2.CaseUpdated:
    return events_pb2.CaseUpdated(case_id=CASE, status="approved", turbine_id=TURBINE,
                                  action=action, version=4, requested_by=requested_by)  # fmt: skip


def with_service(scenario: Callable[[World], Awaitable[None]], sender: Recording | None = None,
                 directory: FakeDirectory | None = None) -> None:  # fmt: skip
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    found = directory or FakeDirectory()
    clock = [NOW]

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        dispatcher = Dispatcher(engine, found, sender or Recording(), SECRET,
                                clock=lambda: clock[0])  # fmt: skip

        def register(server: grpc.aio.Server) -> None:
            notification_pb2_grpc.add_NotificationServiceServicer_to_server(
                NotificationService(engine, FakeAuthorizer(), clock=lambda: clock[0]), server
            )

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = notification_pb2_grpc.NotificationServiceStub(channel)
                await scenario(World(stub, engine, found, dispatcher, clock))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# --- Whom each event reaches


def test_a_submitted_work_order_reaches_who_may_approve_it_never_its_own_hands() -> None:
    sender = Recording()

    async def scenario(world: World) -> None:
        assert await world.hear(consumer.SUBMITTED, submitted(submitted_by=BOTH))
        assert not await world.hear(consumer.SUBMITTED, submitted(submitted_by=BOTH)), "EVT-003"
        assert await world.dispatcher.run_once() == 1
        told = await world.told()
        # Tenant-wide and the turbine's wind farm's managers; never the one who submitted it,
        # who may not approve it (RBAC-004).
        assert set(told) == {MANAGER, FARM_MANAGER}
        assert {(s, p, f, t) for s, p, f, t in world.directory.asked} == {
            (f"service:{SERVICE_NAME}", "maintenance.approve", "dogger-east", TURBINE)
        }, "asked as this service, in the turbine's scope"
        note = told[MANAGER]
        assert (note.kind, note.link) == (rules.APPROVAL_REQUESTED, "/screens/approvals")
        assert WORK_ORDER in note.title and CASE in note.body and TURBINE in note.body
        sent = await world.deliveries()
        assert sent[(MANAGER, "in_app")].status == "delivered"
        assert sent[(MANAGER, "email")].status == "pending", "email waits for its sender"
        assert await world.dispatcher.run_once() == 2
        sent = await world.deliveries()
        assert (sent[(MANAGER, "email")].status, sent[(MANAGER, "email")].provider) == (
            "recorded",
            "record",
        )
        assert {e.user_id for e in sender.kept} == {MANAGER, FARM_MANAGER}
        assert all("/screens/approvals" in e.text for e in sender.kept)
        assert await world.dispatcher.run_once() == 0, "each once"

    with_service(scenario, sender)


def test_a_decision_reaches_the_people_who_drafted_and_submitted_it() -> None:
    async def scenario(world: World) -> None:
        # An agent drafted it for the fault-investigator, which is not a person to tell.
        await world.hear(consumer.SUBMITTED, submitted(drafted_by=PRINCIPAL), "e-sub")
        await world.dispatcher.run_once()
        await world.hear(consumer.DECIDED, decided(events_pb2.APPROVAL_DECISION_REJECTED), "e-dec")
        await world.dispatcher.run_once()
        told = {u: n for u, n in (await world.told()).items() if n.kind == rules.DECISION_MADE}
        assert set(told) == {PLANNER}
        assert told[PLANNER].title == f"Work order {WORK_ORDER} v1 was rejected"

    with_service(scenario)


@pytest.mark.parametrize(
    ("action", "kind"),
    [
        ("investigation_completed", rules.CASE_PENDING_REVIEW),
        ("approved", rules.CASE_APPROVED),
        ("rejected", rules.CASE_REJECTED),
        ("investigation_ended", rules.CASE_NEEDS_ATTENTION),
        ("decision_not_resumed", rules.CASE_NEEDS_ATTENTION),
    ],
)
def test_a_case_moving_reaches_the_person_who_asked(action: str, kind: str) -> None:
    async def scenario(world: World) -> None:
        await world.hear(consumer.CASES, case_updated(action, requested_by="u-operator"))
        await world.dispatcher.run_once()
        told = await world.told()
        assert set(told) == {"u-operator"} and told["u-operator"].kind == kind
        assert told["u-operator"].link == "/screens/investigations"

    with_service(scenario)


def test_a_detection_case_reaches_who_may_review_its_findings_there() -> None:
    async def scenario(world: World) -> None:
        await world.hear(consumer.CASES, case_updated("investigation_completed", requested_by=""))
        await world.dispatcher.run_once()
        assert set(await world.told()) == {ENGINEER}

    with_service(scenario)


def test_a_verification_reaches_the_person_who_asked_with_its_outcome() -> None:
    """FR-MNT-005: the outcome is reported to a person, who closes or reopens the case."""

    async def scenario(world: World) -> None:
        reported = case_updated("verification_reported", requested_by="u-operator")
        reported.work_order_id, reported.verification_outcome = "wo_" + "9" * 20, "not_resolved"
        await world.hear(consumer.CASES, reported)
        await world.dispatcher.run_once()
        told = (await world.told())["u-operator"]
        assert told.kind == rules.CASE_VERIFIED and told.work_order_id == "wo_" + "9" * 20
        assert told.title == f"Case {CASE}: work verified, not resolved"
        assert "still find it" in told.body and told.link == "/screens/investigations"
        unsaid = case_updated("verification_reported")
        with pytest.raises(InvalidEventError):
            await world.hear(consumer.CASES, unsaid, "e-unsaid")

    with_service(scenario)


def test_other_case_changes_tell_nobody() -> None:
    async def scenario(world: World) -> None:
        for n, action in enumerate(
            ("created", "evidence_frozen", "stale", "investigation_started")
        ):
            assert not await world.hear(consumer.CASES, case_updated(action), f"e-{n}")
        assert await world.dispatcher.run_once() == 0

    with_service(scenario)


def test_only_each_events_owner_is_heard_and_old_news_is_kept_as_skipped() -> None:
    async def scenario(world: World) -> None:
        with pytest.raises(InvalidEventError):
            await world.hear(consumer.SUBMITTED, submitted(), producer="approval")
        with pytest.raises(InvalidEventError):
            await world.hear(consumer.DECIDED, decided(), producer="maintenance")
        # A decision from before decisions named their case: there is nothing to say.
        old_style = decided()
        old_style.case_id = ""
        assert not await world.hear(consumer.DECIDED, old_style)
        # Replayed history, or held back too long: nobody is told now.
        assert not await world.hear(consumer.SUBMITTED, submitted(), "e-old",
                                    at=NOW - timedelta(days=2))  # fmt: skip
        assert await world.dispatcher.run_once() == 0
        async with world.engine.connect() as connection:
            (row,) = (await connection.execute(select(HEARD))).all()
        assert row.status == "skipped" and "more than a day" in row.note

    with_service(scenario)


# --- When something is down


def test_an_owner_down_delays_notifications_and_a_day_later_gives_up() -> None:
    async def scenario(world: World) -> None:
        world.directory.down = True
        await world.hear(consumer.SUBMITTED, submitted())
        assert await world.dispatcher.run_once() == 0
        assert await world.told() == {}
        world.clock[0] = NOW + timedelta(seconds=30)
        world.directory.down = False
        assert await world.dispatcher.run_once() == 1, "tried again once due"
        assert set(await world.told()) == {MANAGER, BOTH, FARM_MANAGER}

        # Another, down for over a day: it is given up, saying so.
        world.directory.down = True
        await world.hear(consumer.CASES, case_updated("approved"), "e-2")
        world.clock[0] = NOW + timedelta(days=1, minutes=10)
        await world.dispatcher.run_once()
        async with world.engine.connect() as connection:
            row = (await connection.execute(
                select(HEARD).where(HEARD.c.event_id == "e-2"))).one()  # fmt: skip
        assert row.status == "skipped" and "within a day" in row.note

    with_service(scenario)


def test_an_email_outage_never_holds_up_the_app_and_fails_after_five_tries() -> None:
    async def scenario(world: World) -> None:
        await world.hear(consumer.CASES, case_updated("approved"))
        await world.dispatcher.run_once()
        assert (await world.deliveries())[(ENGINEER, "in_app")].status == "delivered"
        for _ in range(6):
            world.clock[0] += timedelta(minutes=10)
            await world.dispatcher.run_once()
        email = (await world.deliveries())[(ENGINEER, "email")]
        assert (email.status, email.attempts, email.last_error) == (
            "failed",
            5,
            "The provider is down",
        )

    with_service(scenario, Failing())


# --- The API


def test_a_person_sees_only_their_own_and_marks_them_read() -> None:
    async def scenario(world: World) -> None:
        await world.hear(consumer.SUBMITTED, submitted())
        await world.hear(consumer.CASES, case_updated("approved", requested_by=MANAGER), "e-2")
        await world.dispatcher.run_once()
        await world.dispatcher.run_once()
        mine = await world.stub.ListMyNotifications(
            pb.ListMyNotificationsRequest(), metadata=as_caller(MANAGER), timeout=5
        )
        assert mine.unread_count == 2 and len(mine.notifications) == 2
        first = mine.notifications[0]
        assert {(d.channel, d.status) for d in first.deliveries} == {
            ("email", "recorded"),
            ("in_app", "delivered"),
        }
        # Another person's, or another tenant's, are not theirs to see or mark.
        theirs = await world.stub.ListMyNotifications(
            pb.ListMyNotificationsRequest(), metadata=as_caller(PLANNER), timeout=5
        )
        assert len(theirs.notifications) == 0
        stranger = await world.stub.ListMyNotifications(
            pb.ListMyNotificationsRequest(), metadata=as_caller(MANAGER, OTHER), timeout=5
        )
        assert len(stranger.notifications) == 0
        marked = await world.stub.MarkNotificationsRead(
            pb.MarkNotificationsReadRequest(notification_ids=[first.notification_id]),
            metadata=as_caller(PLANNER), timeout=5)  # fmt: skip
        assert marked.marked == 0
        marked = await world.stub.MarkNotificationsRead(
            pb.MarkNotificationsReadRequest(notification_ids=[first.notification_id]),
            metadata=as_caller(MANAGER), timeout=5)  # fmt: skip
        assert marked.marked == 1
        unread = await world.stub.ListMyNotifications(
            pb.ListMyNotificationsRequest(unread_only=True), metadata=as_caller(MANAGER),
            timeout=5)  # fmt: skip
        assert unread.unread_count == 1 and len(unread.notifications) == 1
        everything = await world.stub.MarkNotificationsRead(
            pb.MarkNotificationsReadRequest(), metadata=as_caller(MANAGER), timeout=5
        )
        assert everything.marked == 1
        # An agent or a service has no notifications.
        for caller in (as_caller(MANAGER, actor="agent:maintenance-planner"),
                       as_caller("service:approval", person=False)):  # fmt: skip
            code = await code_of(world.stub.ListMyNotifications(
                pb.ListMyNotificationsRequest(), metadata=caller, timeout=5))  # fmt: skip
            assert code is grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)


def test_deliveries_are_for_auditors_and_name_only_user_ids() -> None:
    async def scenario(world: World) -> None:
        await world.hear(consumer.CASES, case_updated("approved"))
        await world.dispatcher.run_once()
        code = await code_of(
            world.stub.ListDeliveries(
                pb.ListDeliveriesRequest(), metadata=as_caller(MANAGER), timeout=5
            )
        )
        assert code is grpc.StatusCode.PERMISSION_DENIED
        listed = await world.stub.ListDeliveries(
            pb.ListDeliveriesRequest(case_id=CASE), metadata=as_caller(AUDITOR), timeout=5
        )
        assert {(d.user_id, d.delivery.channel, d.delivery.status) for d in listed.deliveries} == {
            (ENGINEER, "in_app", "delivered"),
            (ENGINEER, "email", "pending"),
        }
        pending = await world.stub.ListDeliveries(
            pb.ListDeliveriesRequest(status="pending"), metadata=as_caller(AUDITOR), timeout=5
        )
        assert [d.delivery.channel for d in pending.deliveries] == ["email"]

    with_service(scenario)


def test_an_administrator_sets_the_tenants_rules_and_can_go_back_to_the_defaults() -> None:
    async def scenario(world: World) -> None:
        admin = as_caller(ADMIN)
        got = await world.stub.GetNotificationRules(pb.GetNotificationRulesRequest(),
                                                    metadata=admin, timeout=5)  # fmt: skip
        assert not got.customized and len(got.rules) == len(rules.DEFAULTS)
        mine = [pb.Rule(kind="case_approved", audience="holders:maintenance.submit",
                        channels=["in_app"])]  # fmt: skip
        code = await code_of(
            world.stub.SetNotificationRules(
                pb.SetNotificationRulesRequest(rules=mine, idempotency_key="rules-key-1"),
                metadata=as_caller(MANAGER),
                timeout=5,
            )
        )
        assert code is grpc.StatusCode.PERMISSION_DENIED
        for wrong in (
            pb.Rule(kind="nothing", audience="requester", channels=["in_app"]),
            pb.Rule(kind="case_approved", audience="everyone", channels=["in_app"]),
            pb.Rule(kind="case_approved", audience="drafter", channels=["in_app"]),
            pb.Rule(kind="case_approved", audience="requester", channels=["sms"]),
        ):
            code = await code_of(
                world.stub.SetNotificationRules(
                    pb.SetNotificationRulesRequest(rules=[wrong], idempotency_key="rules-key-0"),
                    metadata=admin,
                    timeout=5,
                )
            )
            assert code is grpc.StatusCode.INVALID_ARGUMENT, wrong
        world.directory.holders_of[("maintenance.submit", "dogger-east")] = [PLANNER]
        done = await world.stub.SetNotificationRules(
            pb.SetNotificationRulesRequest(rules=mine, idempotency_key="rules-key-1"),
            metadata=admin,
            timeout=5,
        )
        assert done.customized and [r.audience for r in done.rules] == [mine[0].audience]
        await world.hear(consumer.CASES, case_updated("approved"))
        await world.hear(consumer.SUBMITTED, submitted(), "e-2")
        await world.dispatcher.run_once()
        told = await world.told()
        assert set(told) == {PLANNER}, "only what the tenant's rules say, and nothing else"
        sent = await world.deliveries()
        assert set(sent) == {(PLANNER, "in_app")}
        async with world.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        actions = [parse_event(r.subject, r.envelope).payload.action for r in rows]  # type: ignore[attr-defined]
        assert actions == ["notification.rules_set"]
        reset = await world.stub.SetNotificationRules(
            pb.SetNotificationRulesRequest(idempotency_key="rules-key-2"), metadata=admin, timeout=5
        )
        assert not reset.customized and len(reset.rules) == len(rules.DEFAULTS)

    with_service(scenario)


def test_nothing_kept_is_evidence_a_reason_or_an_address() -> None:
    """FR-NOT-002: identifiers and links only."""

    async def scenario(world: World) -> None:
        await world.hear(consumer.SUBMITTED, submitted())
        await world.hear(consumer.DECIDED, decided(), "e-2")
        await world.hear(consumer.CASES, case_updated("investigation_completed"), "e-3")
        for _ in range(3):
            await world.dispatcher.run_once()
        async with world.engine.connect() as connection:
            heard = [json.loads(r.facts) for r in (await connection.execute(select(HEARD))).all()]
            notes = (await connection.execute(select(NOTIFICATIONS))).all()
        allowed = {"work_order_id", "proposal_version", "case_id", "turbine_id", "drafted_by",
                   "submitted_by", "decision", "requested_by"}  # fmt: skip
        assert all(set(facts) <= allowed for facts in heard)
        assert notes and all("@" not in f"{n.title} {n.body}" for n in notes)

    with_service(scenario)


def test_a_retried_rule_change_changes_nothing_more() -> None:
    """GRP-007: the same key and rules once; the same key with other rules is refused."""

    async def scenario(world: World) -> None:
        admin = as_caller(ADMIN)
        mine = [pb.Rule(kind="case_approved", audience="requester", channels=["in_app"])]
        for _ in range(2):
            await world.stub.SetNotificationRules(
                pb.SetNotificationRulesRequest(rules=mine, idempotency_key="retry-key-1"),
                metadata=admin, timeout=5,
            )  # fmt: skip
        other = [pb.Rule(kind="case_approved", audience="requester", channels=["email"])]
        code = await code_of(world.stub.SetNotificationRules(
            pb.SetNotificationRulesRequest(rules=other, idempotency_key="retry-key-1"),
            metadata=admin, timeout=5,
        ))  # fmt: skip
        assert code is grpc.StatusCode.INVALID_ARGUMENT
        async with world.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        assert len(rows) == 1, "audited once"

    with_service(scenario)
