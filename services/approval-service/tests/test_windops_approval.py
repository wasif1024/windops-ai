"""Approvals through a real gRPC server and SQL database, with stand-in Authorization,
Maintenance, Investigation and Asset services. SQLite has no row-level security or triggers, so
`make approval-check` proves those on PostgreSQL."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import grpc
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.approval.v1 import approval_pb2 as pb
from windops.approval.v1 import approval_pb2_grpc
from windops.asset.v1 import asset_pb2
from windops.events.v1 import events_pb2
from windops.investigation.v1 import investigation_pb2
from windops.maintenance.v1 import maintenance_pb2
from windops_approval import recheck
from windops_approval.consumer import handle
from windops_approval.service import ApprovalService
from windops_approval.sources import Eligibility
from windops_approval.store import METADATA, SERVICE_NAME
from windops_events import Event, InvalidEventError, parse_event
from windops_grpc import (
    NotFoundError,
    ServiceRuntime,
    Settings,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_outbox import OUTBOX_TABLE, create_outbox_table

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(environment="test", version="test", identity_secret=SECRET, grpc_port=0,
                    http_port=0, shutdown_grace_seconds=1)  # fmt: skip
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
WORK_ORDER, CASE, TURBINE = "wo_" + "a" * 20, "case_" + "b" * 20, "dogger-east-t01"
SNAPSHOT, NEWER = "evs_" + "c" * 20, "evs_" + "d" * 20
PLANNER, MANAGER, OPERATOR = "u-planner", "u-manager", "u-operator"
# A manager who also plans: may approve, but not what they had a hand in (RBAC-004).
BOTH = "u-planning-manager"
AGENT = "agent:maintenance-planner"


def as_caller(who: str, tenant: str = TENANT, actor: str | None = None,
              person: bool = True) -> Any:  # fmt: skip
    identity = VerifiedIdentity(subject=who, tenant_id=tenant, user_id=who if person else None,
                                auth_time=int(time.time()), actor=actor)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


@dataclass
class FakeAuthorizer:
    """Grants (who, permission). An agent is never granted maintenance.approve, as Authorization
    holds it (SEC-009): unless `generous`, which shows this service refuses one by itself."""

    grants: set[tuple[str, str]]
    generous: bool = False
    # Who holds maintenance.approve only by a change they made themselves, and when (RBAC-006).
    self_granted: dict[str, datetime] = field(default_factory=dict)
    since: list[datetime] = field(default_factory=list)

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        identity = current_identity()
        if identity.actor and not self.generous and permission != "investigation.read":
            return False
        return ((identity.user_id or identity.subject), permission) in self.grants

    async def eligible(self, permission: str, since: datetime, wind_farm_id: str = "",
                       turbine_id: str = "") -> Eligibility:  # fmt: skip
        self.since.append(since)
        if not await self.allowed(permission, wind_farm_id, turbine_id):
            return Eligibility(False, "No role grants this permission here", 3)
        granted = self.self_granted.get(str(current_identity().user_id))
        if granted is not None and granted > since:
            reason = "Only a change you made yourself after the request counts not (RBAC-006)"
            return Eligibility(False, reason, 3)
        return Eligibility(True, "Granted by the maintenance-manager role", 3)


@dataclass
class FakeSources:
    """The work order and case as they are now, as the Maintenance and Investigation services
    would give them. None means it cannot be reached."""

    order: maintenance_pb2.WorkOrder | None = field(default_factory=lambda: (
        maintenance_pb2.WorkOrder(
            work_order_id=WORK_ORDER, version=1, revision=4, case_id=CASE, turbine_id=TURBINE,
            evidence_snapshot_id=SNAPSHOT, status="submitted",
            validation=maintenance_pb2.Validation(passed=True, policy_outcome="pass",
                                                  policy_result_id="pol_" + "e" * 20),
        )
    ))  # fmt: skip
    current_case: investigation_pb2.Case | None = field(default_factory=lambda: (
        investigation_pb2.Case(case_id=CASE, turbine_id=TURBINE, status="pending_review",
                               evidence_snapshot_id=SNAPSHOT, evidence_version=1)
    ))  # fmt: skip
    # The work order's history: (action, actor).
    history: list[tuple[str, str]] = field(default_factory=lambda: [
        ("drafted", "service:fault-investigator"), ("validated", MANAGER),
        ("updated", PLANNER), ("submitted", PLANNER),
    ])  # fmt: skip
    asked: list[str] = field(default_factory=list)

    async def work_order(self, work_order_id: str) -> Any:
        self.asked.append(f"work_order:{current_identity().user_id}")
        if self.order is None:
            raise UnavailableError("The Maintenance Service cannot be reached right now")
        return maintenance_pb2.GetWorkOrderResponse(
            work_order=self.order,
            history=[maintenance_pb2.WorkOrderEvent(action=a, actor_id=who)
                     for a, who in self.history],
        )  # fmt: skip

    async def case(self, case_id: str) -> Any:
        if self.current_case is None:
            raise UnavailableError("The Investigation Service cannot be reached right now")
        return self.current_case

    async def turbine(self, turbine_id: str) -> Any:
        if current_identity().tenant_id != TENANT:
            raise NotFoundError("No such turbine")
        return asset_pb2.Turbine(turbine_id=turbine_id, wind_farm_id="dogger-east")


GRANTS = {
    (PLANNER, "investigation.read"), (MANAGER, "investigation.read"),
    (MANAGER, "maintenance.approve"), (OPERATOR, "investigation.read"),
    (BOTH, "investigation.read"), (BOTH, "maintenance.approve"),
    ("service:fault-investigator", "investigation.read"),
}  # fmt: skip


def submitted(version: int = 1, **changes: Any) -> Event:
    payload = events_pb2.WorkOrderSubmitted(
        work_order_id=WORK_ORDER, proposal_version=str(version), case_id=CASE,
        turbine_id=TURBINE, evidence_snapshot_id=SNAPSHOT, submitted_by=PLANNER,
        drafted_by="service:fault-investigator", drafted_by_agent=AGENT,
    )  # fmt: skip
    producer = changes.pop("producer", "maintenance")
    for name, value in changes.items():
        setattr(payload, name, value)
    return Event("workorder.submitted", f"e-{version}-{producer}", TENANT, NOW, producer,
                 payload, "", "")  # fmt: skip


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    sources: FakeSources

    async def requested(self, version: int = 1) -> pb.Approval:
        await handle(self.engine, submitted(version))
        listed = await self.stub.ListApprovals(
            pb.ListApprovalsRequest(statuses=["pending"]), metadata=as_caller(MANAGER), timeout=5
        )
        return listed.approvals[-1]  # type: ignore[no-any-return]

    def decide(self, approval: pb.Approval, decision: str = "approve", who: Any = None,
               reason: str = "", key: str = "", **expected: Any) -> Any:  # fmt: skip
        return self.stub.DecideApproval(
            pb.DecideApprovalRequest(
                approval_id=approval.approval_id, decision=decision, reason=reason,
                expected_proposal_version=expected.get("version", approval.proposal_version),
                expected_evidence_snapshot_id=expected.get(
                    "snapshot", approval.evidence_snapshot_id),
                idempotency_key=key,
            ),
            metadata=who or as_caller(MANAGER), timeout=5,
        )  # fmt: skip

    async def get(self, approval: pb.Approval) -> pb.Approval:
        answer = await self.stub.GetApproval(
            pb.GetApprovalRequest(approval_id=approval.approval_id),
            metadata=as_caller(MANAGER), timeout=5,
        )  # fmt: skip
        return answer.approval  # type: ignore[no-any-return]

    async def events(self, subject: str) -> list[Any]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(select(OUTBOX_TABLE))).all()
        return [parse_event(r.subject, r.envelope).payload for r in rows if r.subject == subject]


def with_service(scenario: Callable[[World], Awaitable[None]], *, generous: bool = False,
                 sources: FakeSources | None = None,
                 authorizer: FakeAuthorizer | None = None) -> None:  # fmt: skip
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    fakes = sources or FakeSources()

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)

        def register(server: grpc.aio.Server) -> None:
            approval_pb2_grpc.add_ApprovalServiceServicer_to_server(
                ApprovalService(engine, authorizer or FakeAuthorizer(GRANTS, generous), fakes,
                                clock=lambda: NOW),
                server,
            )  # fmt: skip

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = approval_pb2_grpc.ApprovalServiceStub(channel)
                await scenario(World(stub, engine, fakes))
        finally:
            await runtime.stop()
            await engine.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_a_submission_opens_one_request_however_often_it_arrives() -> None:
    """EVT-003: a repeated event opens nothing more."""

    async def scenario(world: World) -> None:
        assert await handle(world.engine, submitted())
        assert not await handle(world.engine, submitted())
        approval = await world.get(await world.requested())
        assert (approval.status, approval.proposal_version, approval.submitted_by) == (
            "pending",
            1,
            PLANNER,
        )
        assert (approval.drafted_by_agent, approval.evidence_snapshot_id) == (AGENT, SNAPSHOT)
        assert approval.approval_id.startswith("apr_") and not approval.HasField("decision")

    with_service(scenario)


def test_only_maintenance_may_announce_a_submission() -> None:
    async def scenario(world: World) -> None:
        with pytest.raises(InvalidEventError):
            await handle(world.engine, submitted(producer="inventory"))
        with pytest.raises(InvalidEventError):
            await handle(world.engine, submitted(evidence_snapshot_id=""))

    with_service(scenario)


def test_a_manager_approves_and_the_decision_is_recorded_and_announced() -> None:
    """FR-APR-002: approver, decision, time, policy context, proposal version and snapshot.
    EVD-009: the work order and case were read again, as the manager."""

    async def scenario(world: World) -> None:
        approval = await world.requested()
        decided = (await world.decide(approval, reason="Cooler fan fits the findings")).approval
        assert decided.status == "approved"
        d = decided.decision
        assert (d.outcome, d.decided_by, d.decided_at.ToDatetime(UTC)) == ("approved", MANAGER, NOW)
        assert (d.proposal_version, d.evidence_snapshot_id) == (1, SNAPSHOT)
        assert (d.policy.policy_outcome, d.policy.case_status) == ("pass", "pending_review")
        assert (d.policy.policy_result_id, d.policy.work_order_revision) == ("pol_" + "e" * 20, 4)
        assert world.sources.asked == [f"work_order:{MANAGER}"]
        [announced] = await world.events("approval.decided")
        assert (announced.approval_id, announced.work_order_id, announced.proposal_version,
                announced.evidence_snapshot_id, announced.decided_by) == (
            approval.approval_id, WORK_ORDER, "1", SNAPSHOT, MANAGER)  # fmt: skip
        assert announced.decision == events_pb2.APPROVAL_DECISION_APPROVED
        audited = await world.events("audit.requested.approval")
        assert [(a.action, a.actor_id) for a in audited] == [("approval.approved", MANAGER)]
        # Decided once: never again, either way.
        again = await code_of(world.decide(approval, "reject", reason="Changed my mind"))
        assert again == grpc.StatusCode.FAILED_PRECONDITION
        assert (await world.get(approval)).decision.outcome == "approved"

    with_service(scenario)


def test_a_rejection_needs_a_reason() -> None:
    async def scenario(world: World) -> None:
        approval = await world.requested()
        assert await code_of(world.decide(approval, "reject")) == grpc.StatusCode.INVALID_ARGUMENT
        rejected = (await world.decide(approval, "reject", reason="Wrong component")).approval
        assert (rejected.status, rejected.decision.reason) == ("rejected", "Wrong component")
        [announced] = await world.events("approval.decided")
        assert announced.decision == events_pb2.APPROVAL_DECISION_REJECTED

    with_service(scenario)


@pytest.mark.parametrize("generous", [False, True])
def test_no_agent_ever_decides(generous: bool) -> None:
    """FR-APR-001, SEC-009: refused before Authorization is asked, even when it would allow it,
    and audited as the agent on its principal's behalf."""

    async def scenario(world: World) -> None:
        approval = await world.requested()
        for who in (as_caller(MANAGER, actor=AGENT),
                    as_caller("service:fault-investigator", actor=AGENT, person=False),
                    as_caller("service:workflow", person=False)):  # fmt: skip
            assert await code_of(world.decide(approval, who=who)) == (
                grpc.StatusCode.PERMISSION_DENIED
            )
        assert (await world.get(approval)).status == "pending"
        assert world.sources.asked == []
        assert await world.events("approval.decided") == []
        audited = await world.events("audit.requested.approval")
        assert [(a.action, a.actor_type, a.resource_id) for a in audited] == [
            ("authorization.denied", "agent", "maintenance.approve")] * 2 + [
            ("authorization.denied", "service", "maintenance.approve")]  # fmt: skip
        assert audited[0].actor_id == AGENT and audited[0].on_behalf_of == MANAGER

    with_service(scenario, generous=generous)


def test_a_person_without_maintenance_approve_is_refused() -> None:
    async def scenario(world: World) -> None:
        approval = await world.requested()
        assert await code_of(world.decide(approval, who=as_caller(PLANNER))) == (
            grpc.StatusCode.PERMISSION_DENIED
        )
        # Another tenant cannot even see it.
        assert await code_of(world.decide(approval, who=as_caller(MANAGER, OTHER))) == (
            grpc.StatusCode.NOT_FOUND
        )
        assert (await world.get(approval)).status == "pending"

    with_service(scenario)


def test_a_decision_on_what_was_not_reviewed_is_refused() -> None:
    async def scenario(world: World) -> None:
        approval = await world.requested()
        for expected in ({"version": 2}, {"snapshot": NEWER}):
            assert await code_of(world.decide(approval, **expected)) == (
                grpc.StatusCode.FAILED_PRECONDITION
            )
        assert (await world.get(approval)).status == "pending"

    with_service(scenario)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"version": 2}, "version 2"),
        ({"status": "withdrawn"}, "now withdrawn"),
        ({"evidence_snapshot_id": NEWER}, NEWER),
    ],
)
def test_a_superseded_proposal_cannot_be_approved(change: dict[str, Any], expected: str) -> None:
    """FR-APR-003: the request is superseded, kept with why, and never decided."""

    async def scenario(world: World) -> None:
        approval = await world.requested()
        assert world.sources.order is not None
        for name, value in change.items():
            setattr(world.sources.order, name, value)
        assert await code_of(world.decide(approval)) == grpc.StatusCode.FAILED_PRECONDITION
        kept = await world.get(approval)
        assert kept.status == "superseded" and expected in kept.superseded_reason
        assert await world.events("approval.decided") == []
        audited = await world.events("audit.requested.approval")
        assert [(a.action, a.outcome) for a in audited] == [("approval.refused", "denied")]

    with_service(scenario)


@pytest.mark.parametrize(
    ("change", "expected"),
    [({"evidence_snapshot_id": NEWER}, NEWER), ({"status": "closed"}, "now closed")],
)
def test_a_superseded_snapshot_cannot_be_approved(change: dict[str, Any], expected: str) -> None:
    """FR-APR-003, EVD-009: the case's evidence moved on, or the case ended."""

    async def scenario(world: World) -> None:
        approval = await world.requested()
        assert world.sources.current_case is not None
        for name, value in change.items():
            setattr(world.sources.current_case, name, value)
        assert await code_of(world.decide(approval)) == grpc.StatusCode.FAILED_PRECONDITION
        kept = await world.get(approval)
        assert kept.status == "superseded" and expected in kept.superseded_reason

    with_service(scenario)


@pytest.mark.parametrize("down", ["order", "current_case"])
def test_a_decision_fails_closed_when_it_cannot_be_rechecked(down: str) -> None:
    """EVD-010: nothing is decided, nothing announced, and the request stays pending."""

    async def scenario(world: World) -> None:
        approval = await world.requested()
        setattr(world.sources, down, None)
        assert await code_of(world.decide(approval)) == grpc.StatusCode.UNAVAILABLE
        assert (await world.get(approval)).status == "pending"
        assert await world.events("approval.decided") == []
        audited = await world.events("audit.requested.approval")
        assert [(a.action, a.outcome) for a in audited] == [("approval.refused", "failed")]

    with_service(scenario)


def test_a_new_version_supersedes_the_pending_one() -> None:
    async def scenario(world: World) -> None:
        first = await world.requested(1)
        second = await world.requested(2)
        assert second.proposal_version == 2
        kept = await world.get(first)
        assert kept.status == "superseded" and "Version 2" in kept.superseded_reason
        # An older version arriving late opens nothing.
        assert not await handle(world.engine, submitted(1))

    with_service(scenario)


def test_a_retried_decision_returns_the_first_answer() -> None:
    """GRP-007."""

    async def scenario(world: World) -> None:
        approval = await world.requested()
        first = (await world.decide(approval, key="k-1")).approval
        again = (await world.decide(approval, key="k-1")).approval
        assert first == again
        assert len(await world.events("approval.decided")) == 1
        reused = await code_of(world.decide(approval, "reject", reason="no", key="k-1"))
        assert reused == grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_the_queue_lists_oldest_first_and_filters_by_status() -> None:
    async def scenario(world: World) -> None:
        approval = await world.requested()
        await world.decide(approval)
        await handle(world.engine, submitted(2))
        listed = await world.stub.ListApprovals(
            pb.ListApprovalsRequest(), metadata=as_caller(OPERATOR), timeout=5
        )
        assert [a.status for a in listed.approvals] == ["approved", "pending"]
        assert await code_of(world.stub.ListApprovals(
            pb.ListApprovalsRequest(statuses=["maybe"]), metadata=as_caller(MANAGER), timeout=5,
        )) == grpc.StatusCode.INVALID_ARGUMENT  # fmt: skip

    with_service(scenario)


def test_a_policy_result_that_allows_no_decision_refuses_without_superseding() -> None:
    row = type("Row", (), {"proposal_version": 1, "evidence_snapshot_id": SNAPSHOT})()
    order = maintenance_pb2.WorkOrder(version=1, status="submitted",
                                      evidence_snapshot_id=SNAPSHOT)  # fmt: skip
    case = investigation_pb2.Case(status="investigating", evidence_snapshot_id=SNAPSHOT)
    found = recheck.refusal(row, order, case)
    assert found is not None and not found.supersedes
    order.validation.policy_outcome = "escalate"
    assert recheck.refusal(row, order, case) is None


@pytest.mark.parametrize(
    "history",
    [
        [("drafted", BOTH), ("submitted", PLANNER)],
        [("drafted", PLANNER), ("updated", BOTH), ("submitted", PLANNER)],
        [("drafted", PLANNER), ("submitted", BOTH)],
    ],
    ids=["drafted", "changed", "submitted"],
)
def test_nobody_decides_what_they_drafted_changed_or_submitted(
    history: list[tuple[str, str]],
) -> None:
    """RBAC-004: read from the work order's history at decision time. The refusal is audited,
    the request stays pending, and another manager decides it, recorded with who was excluded."""

    async def scenario(world: World) -> None:
        world.sources.history = history
        approval = await world.requested()
        for decision, reason in (("approve", ""), ("reject", "Not needed")):
            refused = world.decide(approval, decision, who=as_caller(BOTH), reason=reason)
            assert await code_of(refused) == grpc.StatusCode.PERMISSION_DENIED
        assert (await world.get(approval)).status == "pending"
        audited = await world.events("audit.requested.approval")
        assert [(a.action, a.actor_id) for a in audited] == [("approval.refused", BOTH)] * 2
        decided = (await world.decide(approval)).approval
        assert decided.status == "approved" and BOTH in decided.decision.requesters
        assert MANAGER not in decided.decision.requesters
        assert decided.decision.policy.approval_rules == "separation-of-duties-2"
        assert decided.decision.policy.permissions_version == 3

    with_service(scenario)


def test_an_agents_draft_belongs_to_the_person_it_drafted_for() -> None:
    """RBAC-008: the person who started the investigation is a requester of the agent's draft,
    whoever submits it. Validating changes nothing, so a manager who only validated may decide."""

    async def scenario(world: World) -> None:
        world.sources.history = [("drafted", BOTH), ("validated", MANAGER),
                                 ("submitted", PLANNER)]  # fmt: skip
        await handle(world.engine, submitted(drafted_by=BOTH))
        [approval] = (
            await world.stub.ListApprovals(
                pb.ListApprovalsRequest(), metadata=as_caller(MANAGER), timeout=5
            )
        ).approvals
        assert approval.drafted_by_agent == AGENT
        refused = await code_of(world.decide(approval, who=as_caller(BOTH)))
        assert refused == grpc.StatusCode.PERMISSION_DENIED
        decided = (await world.decide(approval)).approval
        assert decided.decision.requesters == sorted([BOTH, PLANNER])

    with_service(scenario)


def test_eligibility_is_held_now_and_not_by_your_own_change_since_the_request() -> None:
    """RBAC-005, RBAC-006: Authorization is asked as of the submission; a role the manager
    gave themselves after it does not count, and they are told why."""
    from datetime import timedelta

    authorizer = FakeAuthorizer(GRANTS, self_granted={MANAGER: NOW + timedelta(hours=1)})

    async def scenario(world: World) -> None:
        approval = await world.requested()
        with pytest.raises(grpc.aio.AioRpcError) as raised:
            await world.decide(approval)
        assert raised.value.code() == grpc.StatusCode.PERMISSION_DENIED
        assert "RBAC-006" in str(raised.value.details())
        assert authorizer.since and all(s == NOW for s in authorizer.since)
        assert (await world.get(approval)).status == "pending"
        assert world.sources.asked == []

    with_service(scenario, authorizer=authorizer)
