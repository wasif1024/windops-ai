"""Each case's investigation: started once its evidence is frozen, followed, and watched live
(M8.11, API-003), with a stand-in Agent Runtime and Identity Service."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, text, update
from test_windops_investigation import NOW, TENANT, World, as_user, code_of, with_service
from windops.agent_runtime.v1 import agent_runtime_pb2 as rt
from windops.events.v1 import events_pb2
from windops.investigation.v1 import investigation_pb2 as pb
from windops_events import Event
from windops_grpc import UnavailableError, current_identity
from windops_investigation import investigations
from windops_investigation.consumer import handle
from windops_investigation.investigations import Keeper
from windops_investigation.store import CASES, INVESTIGATIONS

SECRET = "x" * 32
TURBINE = "dogger-east-t01"
# Built from parts, so no scanner mistakes it for a real token.
TOKEN = "eyJ" + "hbGciOiJSUzI1NiJ9." + "person-token-one"
NEWER_TOKEN = "eyJ" + "hbGciOiJSUzI1NiJ9." + "person-token-two"
STEPS = (
    "receive", "validate", "snapshot", "plan", "specialists", "documents", "research", "impact",
    "aggregate", "validate_output", "policy_checks", "recommend", "conclude",
)  # fmt: skip


def ts(value: datetime) -> Timestamp:
    stamp = Timestamp()
    stamp.FromDatetime(value)
    return stamp


@dataclass
class FakeRuntime:
    runs: dict[str, rt.Run] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    started: list[dict[str, Any]] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    down: bool = False

    def _ask(self) -> None:
        self.callers.append(current_identity().subject)
        if self.down:
            raise UnavailableError("The Agent Runtime cannot be reached right now")

    async def start_run(self, **request: Any) -> rt.Run:
        self._ask()
        key = request["key"]
        if key not in self.keys:
            run_id = f"run_{len(self.keys):020d}"
            self.keys[key] = run_id
            self.started.append(request)
            self.runs[run_id] = rt.Run(
                run_id=run_id, case_id=request["case_id"], snapshot_id=request["snapshot_id"],
                status="queued", total_steps=len(STEPS),
            )  # fmt: skip
        return self.runs[self.keys[key]]

    async def get_run(self, run_id: str) -> rt.Run:
        self._ask()
        return self.runs[run_id]

    async def cancel_run(self, run_id: str, reason: str) -> rt.Run:
        self._ask()
        self.runs[run_id].cancel_requested = True
        return self.runs[run_id]

    def advance(self, run_id: str, steps: int, status: str = "running", reason: str = "") -> None:
        run = self.runs[run_id]
        for position in range(len(run.steps) + 1, steps + 1):
            run.steps.append(rt.Step(position=position, name=STEPS[position - 1], attempt=1,
                                     finished_at=ts(NOW)))  # fmt: skip
        run.status, run.reason = status, reason


@dataclass
class FakeDelegations:
    expires: datetime = NOW + timedelta(hours=4)
    users: list[tuple[str, str, str]] = field(default_factory=list)
    faults: list[tuple[str, str]] = field(default_factory=list)

    async def for_user(self, case_id: str, subject_token: str) -> tuple[str, datetime]:
        self.users.append((case_id, subject_token, current_identity().user_id or ""))
        return f"dlg_user{len(self.users):016d}", self.expires

    async def for_fault_investigator(self, case_id: str) -> tuple[str, datetime]:
        self.faults.append((case_id, current_identity().subject))
        return f"dlg_fault{len(self.faults):015d}", self.expires


@dataclass
class Setup:
    runtime: FakeRuntime = field(default_factory=FakeRuntime)
    delegations: FakeDelegations = field(default_factory=FakeDelegations)

    def keeper(self, world: World, mode: str = "rehearse") -> Keeper:
        return Keeper(world.engine, self.runtime, self.delegations, SECRET, mode,
                      clock=lambda: NOW)  # fmt: skip

    def run(self, scenario: Any, agents: str = "rehearse", **options: Any) -> None:
        with_service(scenario, delegations=self.delegations, agents=agents, poll_seconds=0.01,
                     **options)  # fmt: skip


async def investigate(world: World, key: str = "investigate-1", token: str = TOKEN,
                      user: str = "u-operator") -> pb.Case:  # fmt: skip
    answer = await world.stub.CreateCase(
        pb.CreateCaseRequest(turbine_id=TURBINE, title="Gearbox noise", severity="high",
                             idempotency_key=key, investigate=True, subject_token=token),
        metadata=as_user(user), timeout=5,
    )  # fmt: skip
    case: pb.Case = answer.case
    return case


async def freeze(world: World, case_id: str) -> None:
    """Stand in for the evidence builder: the case's first snapshot is frozen."""
    async with world.engine.begin() as connection:
        await connection.execute(
            update(CASES).where(CASES.c.case_id == case_id)
            .values(evidence_snapshot_id=f"snap_{case_id[-12:]}", evidence_version=1,
                    evidence_wanted_at=None)
        )  # fmt: skip


async def case_of(world: World, case_id: str, user: str = "u-operator") -> pb.GetCaseResponse:
    got: pb.GetCaseResponse = await world.stub.GetCase(
        pb.GetCaseRequest(case_id=case_id), metadata=as_user(user), timeout=5
    )
    return got


async def watch(world: World, case_id: str, after: int = 0,
                user: str = "u-operator") -> list[pb.WatchInvestigationResponse]:  # fmt: skip
    stream: AsyncIterator[pb.WatchInvestigationResponse] = world.stub.WatchInvestigation(
        pb.WatchInvestigationRequest(case_id=case_id, after_sequence=after),
        metadata=as_user(user), timeout=10,
    )  # fmt: skip
    return [item async for item in stream]


def test_a_person_gets_the_case_at_once_and_its_investigation_follows_its_run() -> None:
    """API-003 and SRS 5.4 step 13: the case's ID first; the run starts once the evidence is
    frozen, under the person's delegation; a completed run leaves the case pending review."""
    setup = Setup()

    async def scenario(world: World) -> None:
        case = await investigate(world)
        assert (case.status, case.investigation_status, case.run_id) == ("open", "waiting", "")
        assert setup.delegations.users == [(case.case_id, TOKEN, "u-operator")]
        keeper = setup.keeper(world)
        assert await keeper.run_once() == 0, "no run before the evidence is frozen"
        await freeze(world, case.case_id)
        assert await keeper.run_once() == 1
        (started,) = setup.runtime.started
        assert started == {
            "case_id": case.case_id, "snapshot_id": f"snap_{case.case_id[-12:]}",
            "parameters": {"team": "true", "rehearse": "true", "turbine_id": TURBINE},
            "requested_by": "user:u-operator", "delegation_id": "dlg_user0000000000000001",
            "key": f"investigate-{case.case_id}",
        }  # fmt: skip
        assert set(setup.runtime.callers) == {investigations.KEEPER}, "as the service itself"
        got = await case_of(world, case.case_id)
        run_id = got.case.run_id
        assert (got.case.status, got.case.investigation_status) == ("investigating", "running")

        setup.runtime.advance(run_id, 5)
        await keeper.run_once()
        await keeper.run_once()  # Following again adds nothing twice.
        setup.runtime.advance(run_id, 13, "completed")
        await keeper.run_once()
        got = await case_of(world, case.case_id)
        assert (got.case.status, got.case.investigation_status) == ("pending_review", "completed")
        assert [h.action for h in got.history] == [
            "created", "investigation_started", "investigation_completed",
        ]  # fmt: skip

        streamed = await watch(world, case.case_id)
        assert streamed[-1].WhichOneof("item") == "finished"
        events = [s.event for s in streamed[:-1]]
        assert [e.sequence for e in events] == list(range(1, len(events) + 1))
        assert [(e.kind, e.step or e.status) for e in events] == [
            ("requested", ""), ("started", ""), ("case_status", "investigating"),
            *[("step", name) for name in STEPS],
            ("run_ended", "completed"), ("case_status", "pending_review"),
        ]  # fmt: skip
        assert {e.run_id for e in events if e.kind in ("step", "started")} == {run_id}
        assert events[3].position == 1 and events[3].total_steps == 13

        # Resuming after an event ID sends only what came after it.
        resumed = await watch(world, case.case_id, after=5)
        assert [s.event.sequence for s in resumed[:-1]] == [e.sequence for e in events[5:]]
        assert resumed[-1].finished

        # The person's token was passed on, never kept.
        async with world.engine.connect() as connection:
            tables = (await connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )).scalars().all()  # fmt: skip
            for table in tables:
                for row in (await connection.execute(text(f"SELECT * FROM {table}"))).all():  # noqa: S608  # Our own table names.
                    assert "person-token" not in repr(row), table

    setup.run(scenario)


def test_a_retry_with_a_newer_token_is_the_same_request() -> None:
    """GRP-007: the browser may resend with a refreshed token, and gets the same case."""
    setup = Setup()

    async def scenario(world: World) -> None:
        first = await investigate(world, key="same-key")
        again = await investigate(world, key="same-key", token=NEWER_TOKEN)
        assert again.case_id == first.case_id
        assert len(setup.delegations.users) == 1
        other = await code_of(world.stub.CreateCase(
            pb.CreateCaseRequest(turbine_id=TURBINE, title="Another", severity="high",
                                 idempotency_key="same-key", investigate=True,
                                 subject_token=TOKEN),
            metadata=as_user(), timeout=5,
        ))  # fmt: skip
        assert other == grpc.StatusCode.INVALID_ARGUMENT

    setup.run(scenario)


def test_investigating_needs_a_token_permission_and_agents_turned_on() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        def ask(user: str = "u-operator", **fields: Any) -> Any:
            request = pb.CreateCaseRequest(turbine_id=TURBINE, title="Noise", severity="high",
                                           **fields)  # fmt: skip
            return code_of(world.stub.CreateCase(request, metadata=as_user(user), timeout=5))

        assert await ask(investigate=True) == grpc.StatusCode.INVALID_ARGUMENT
        assert await ask(subject_token=TOKEN) == grpc.StatusCode.INVALID_ARGUMENT
        assert await ask("u-auditor", investigate=True, subject_token=TOKEN) == (
            grpc.StatusCode.PERMISSION_DENIED
        )
        assert setup.delegations.users == [], "no delegation for a refused request"
        # A case without an investigation is as before, and its watch ends at once.
        assert await ask() == grpc.StatusCode.OK
        (plain,) = [c for c in (await world.active()) if c.title == "Noise"]
        assert plain.investigation_status == ""
        assert [s.WhichOneof("item") for s in await watch(world, plain.case_id)] == ["finished"]

    setup.run(scenario)

    async def turned_off(world: World) -> None:
        refused = await code_of(world.stub.CreateCase(
            pb.CreateCaseRequest(turbine_id=TURBINE, title="Noise", severity="high",
                                 investigate=True, subject_token=TOKEN),
            metadata=as_user(), timeout=5,
        ))  # fmt: skip
        assert refused == grpc.StatusCode.FAILED_PRECONDITION

    setup.run(turned_off, agents="off")


def test_a_case_from_detections_is_investigated_as_the_fault_investigator() -> None:
    """ADR 0019: no person asked, so the tenant's fault-investigator acts. A run that fails puts
    the case back to open, with why."""
    setup = Setup()

    async def scenario(world: World) -> None:
        async def detect(detection_id: str) -> None:
            payload = events_pb2.FaultDetected(
                detection_id=detection_id, turbine_id=TURBINE, rule_version="1",
                telemetry_snapshot_id=f"tsnap_{detection_id}", rule_id="gearbox-oil-residual",
                severity="high", rule_content_hash="h" * 64,
            )  # fmt: skip
            event = Event("fault.detected", f"e-{detection_id}", TENANT, NOW, "fault-detection",
                          payload, "", "")  # fmt: skip
            await handle(world.engine, event, investigate=True)

        await detect("d1")
        await detect("d2")  # Joins the case, and asks for nothing more.
        (case,) = await world.active()
        assert case.investigation_status == "waiting"
        await freeze(world, case.case_id)
        keeper = setup.keeper(world)
        await keeper.run_once()
        assert setup.delegations.faults == [(case.case_id, investigations.KEEPER)]
        (started,) = setup.runtime.started
        assert started["requested_by"] == "service:investigation"
        run_id = setup.runtime.keys[f"investigate-{case.case_id}"]
        setup.runtime.advance(run_id, 4, "failed", "The Supervisor could not plan")
        await keeper.run_once()
        got = await case_of(world, case.case_id)
        assert (got.case.status, got.case.investigation_status) == ("open", "ended")
        assert got.history[-1].action == "investigation_ended"
        assert got.history[-1].note == "failed: The Supervisor could not plan"
        events = [s.event for s in (await watch(world, case.case_id))[:-1]]
        assert [(e.kind, e.status) for e in events[-2:]] == [
            ("run_ended", "failed"), ("case_status", "open"),
        ]  # fmt: skip

    setup.run(scenario)


def test_a_cancelled_case_stops_its_investigation() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        keeper = setup.keeper(world)
        waiting = await investigate(world, key="waiting")
        running = await investigate(world, key="running")
        await freeze(world, running.case_id)
        await keeper.run_once()
        for case in (waiting, running):
            await world.stub.CancelCase(
                pb.CancelCaseRequest(case_id=case.case_id, reason="Duplicate"),
                metadata=as_user("u-engineer"),
                timeout=5,
            )
        await keeper.run_once()
        assert (await case_of(world, waiting.case_id)).case.investigation_status == "not_started"
        run_id = (await case_of(world, running.case_id)).case.run_id
        assert setup.runtime.runs[run_id].cancel_requested
        setup.runtime.advance(run_id, 6, "cancelled", "Cancelled: The case was cancelled")
        await keeper.run_once()
        got = await case_of(world, running.case_id)
        assert (got.case.status, got.case.investigation_status) == ("cancelled", "ended")
        assert got.history[-1].action == "cancelled", "a cancelled case stays cancelled"

    setup.run(scenario)


def test_an_unreachable_runtime_or_a_lapsed_delegation() -> None:
    """SRS 7.7: the run starts once the runtime is back. A delegation that runs out first starts
    nothing, and says so."""
    setup = Setup()

    async def scenario(world: World) -> None:
        keeper = setup.keeper(world)
        case = await investigate(world)
        await freeze(world, case.case_id)
        setup.runtime.down = True
        assert await keeper.run_once() == 0
        assert (await case_of(world, case.case_id)).case.investigation_status == "waiting"
        setup.runtime.down = False
        assert await keeper.run_once() == 1

        setup.delegations.expires = NOW + timedelta(minutes=10)
        late = await investigate(world, key="late")
        await freeze(world, late.case_id)
        await keeper.run_once()
        got = await case_of(world, late.case_id)
        assert (got.case.status, got.case.investigation_status) == ("open", "not_started")
        assert got.history[-1].action == "investigation_not_started"
        assert len(setup.runtime.started) == 1

    setup.run(scenario)


def test_only_readers_watch_and_a_watch_ends_to_be_called_again() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case = await investigate(world)
        assert await code_of(watch(world, case.case_id, user="u-nobody")) == (
            grpc.StatusCode.NOT_FOUND
        )
        assert await code_of(watch(world, "case_missing")) == grpc.StatusCode.NOT_FOUND
        assert await code_of(watch(world, case.case_id, after=-1)) == (
            grpc.StatusCode.INVALID_ARGUMENT
        )
        # Still waiting: the watch sends what there is, then ends without "finished".
        streamed = await watch(world, case.case_id, user="u-dogger")
        assert [s.event.kind for s in streamed] == ["requested"]

    setup.run(scenario, watch_seconds=0.2)


def test_two_keepers_record_each_step_once() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case = await investigate(world)
        await freeze(world, case.case_id)
        first, second = setup.keeper(world), setup.keeper(world)
        await first.run_once()
        await second.run_once()
        run_id = setup.runtime.keys[f"investigate-{case.case_id}"]
        setup.runtime.advance(run_id, 13, "completed")
        await first.run_once()
        await second.run_once()
        events = [s.event for s in (await watch(world, case.case_id))[:-1]]
        assert len(events) == len({(e.kind, e.position, e.status) for e in events}) == 18
        async with world.engine.connect() as connection:
            (row,) = (await connection.execute(select(INVESTIGATIONS))).all()
        assert row.status == "completed"

    setup.run(scenario)


def test_an_investigation_whose_case_is_gone_is_no_longer_followed() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        keeper = setup.keeper(world)
        case = await investigate(world)
        await freeze(world, case.case_id)
        await keeper.run_once()
        async with world.engine.begin() as connection:
            await connection.execute(CASES.delete().where(CASES.c.case_id == case.case_id))
        asked = len(setup.runtime.callers)
        assert await keeper.run_once() == 1
        assert await keeper.run_once() == 0
        assert len(setup.runtime.callers) == asked, "the runtime is not asked about it"
        async with world.engine.connect() as connection:
            (row,) = (await connection.execute(select(INVESTIGATIONS))).all()
        assert (row.status, row.note) == ("ended", "The case is gone")

    setup.run(scenario)
