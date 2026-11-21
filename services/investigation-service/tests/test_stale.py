"""The stale rules (SRS 8.5, EVD-007, EVD-008): each rule on its own, then each applied to a real
case in a SQL database, announced with the rule, and cleared by a new snapshot."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from test_evidence import NOW, TENANT, TURBINE, Bench, with_bench
from windops.asset.v1 import asset_pb2
from windops.events.v1 import events_pb2
from windops.inventory.v1 import inventory_pb2
from windops_events import Event, InvalidEventError, RetryLaterError, parse_event
from windops_grpc import UnavailableError, current_identity
from windops_investigation import stale, staleness
from windops_outbox import OUTBOX_TABLE

SECRET = "x" * 32
ORDER = "wo_" + "a" * 20


# --- Each rule on its own


def test_severity_goes_stale_only_once_evidence_is_frozen_and_it_rises() -> None:
    assert staleness.severity_raised("medium", "high", 1) is not None
    assert staleness.severity_raised("medium", "high", 0) is None
    assert staleness.severity_raised("high", "high", 2) is None


def test_an_alarm_matters_when_it_becomes_active_at_a_correlated_severity_and_is_new() -> None:
    later, earlier = NOW + timedelta(minutes=5), NOW - timedelta(minutes=5)
    active = events_pb2.AlarmChanged(alarm_id="alm_1", turbine_id=TURBINE, code="GBX_HOT",
                                     state=events_pb2.ALARM_STATE_ACTIVE, severity="high",
                                     version=3)  # fmt: skip
    assert staleness.alarm(active, {}, later, NOW) is not None
    assert staleness.alarm(active, {}, earlier, NOW) is None, "before the snapshot: judged by it"
    assert staleness.alarm(active, {"alm_1": "3"}, later, NOW) is None, "the snapshot holds it"
    assert staleness.alarm(active, {"alm_1": "2"}, later, NOW) is not None, "it changed since"
    low = events_pb2.AlarmChanged(alarm_id="alm_2", state=events_pb2.ALARM_STATE_ACTIVE,
                                  severity="medium", version=1)  # fmt: skip
    assert staleness.alarm(low, {}, later, NOW) is None
    cleared = events_pb2.AlarmChanged(alarm_id="alm_1", state=events_pb2.ALARM_STATE_CLEARED,
                                      severity="high", version=4)  # fmt: skip
    assert staleness.alarm(cleared, {}, later, NOW) is None


def test_a_replaced_document_makes_only_a_proposal_that_cites_it_stale() -> None:
    class Proposal:
        work_order_id, procedures = ORDER, ["kn:GBX-OIL-01@3#12"]

    found = staleness.procedure("GBX-OIL-01", "GBX-OIL-01@4", [Proposal()])
    assert found is not None and "GBX-OIL-01@4" in found.detail
    assert staleness.procedure("YAW-02", "YAW-02@2", [Proposal()]) is None


def test_only_a_material_turbine_change_counts() -> None:
    frozen = {"status": "operating", "wind_farm_id": "f", "serial_number": "s",
              "model": {"model": "m"}, "version": "3", "name": "T1"}  # fmt: skip
    assert staleness.asset(frozen, {**frozen, "name": "Renamed", "version": "4"}) is None
    status = staleness.asset(frozen, {**frozen, "status": "maintenance"})
    assert status is not None and "operating to maintenance" in status.detail
    model = staleness.asset(frozen, {**frozen, "model": {"model": "n"}})
    assert model is not None and "model" in model.detail


@pytest.mark.parametrize(
    ("submitted", "now", "stale"),
    [
        ("available", "available", False),
        ("available", "short", True),
        ("short", "short", False),
        ("short", "retired", True),
        ("available", "incompatible", True),
    ],
)
def test_a_part_goes_stale_when_it_is_no_longer_available(submitted: str, now: str,
                                                          stale: bool) -> None:  # fmt: skip
    found = staleness.inventory({"P-1": submitted}, {"P-1": now}, ORDER)
    assert (found is not None) is stale


def test_age_limits_follow_severity() -> None:
    assert staleness.age("high", NOW, NOW + timedelta(hours=24)) is None
    assert staleness.age("high", NOW, NOW + timedelta(hours=25)) is not None
    assert staleness.age("medium", NOW, NOW + timedelta(hours=25)) is None
    assert staleness.age("low", NOW, NOW + timedelta(hours=169)) is not None


# --- Applied to a case


class FakeOwners:
    def __init__(self) -> None:
        # As the evidence builder's fake froze it: no status.
        self.status, self.stock, self.down = "", "available", False
        self.callers: list[str] = []

    async def turbine(self, turbine_id: str) -> Any:
        self.callers.append(current_identity().subject)
        if self.down:
            raise UnavailableError("The Asset Service cannot be reached right now")
        return asset_pb2.Turbine(turbine_id=turbine_id, wind_farm_id="dogger-east", version=3,
                                 status=self.status)  # fmt: skip

    async def parts(self, turbine: Any, lines: list[tuple[str, int]]) -> list[Any]:
        return [inventory_pb2.PartCheck(part_number=n, quantity=q, status=self.stock)
                for n, q in lines]  # fmt: skip


async def frozen_case(bench: Bench) -> Any:
    await bench.detect("d1", "medium")
    await bench.builder.run_once(NOW)
    return await bench.case()


async def submit(bench: Bench, case: Any, version: int = 1) -> bool:
    payload = events_pb2.WorkOrderSubmitted(
        work_order_id=ORDER, proposal_version=str(version), case_id=case.case_id,
        turbine_id=TURBINE, evidence_snapshot_id=case.evidence_snapshot_id,
        submitted_by="u-planner", procedures=["kn:GBX-OIL-01@3#12"],
        parts=[events_pb2.ProposedPart(part_number="P-1", quantity=1, status="available")],
    )  # fmt: skip
    return await stale.on_submitted(
        bench.engine,
        Event("workorder.submitted", f"s-{version}", TENANT, NOW, "maintenance", payload, "", ""),
    )


def evidence_changed(source: str, record_id: str, version: str) -> Event:
    payload = events_pb2.EvidenceChanged(source_service=source, record_id=record_id,
                                         version=version)  # fmt: skip
    return Event("evidence.changed", f"e-{record_id}-{version}", TENANT, NOW, source, payload,
                 "", "")  # fmt: skip


async def announced(bench: Bench) -> list[Any]:
    async with bench.engine.connect() as connection:
        rows = (await connection.execute(select(OUTBOX_TABLE))).all()
    events = [parse_event(r.subject, r.envelope).payload for r in rows]
    return [e for e in events if isinstance(e, events_pb2.CaseUpdated) and e.action == "stale"]


def test_a_raised_severity_makes_frozen_evidence_stale_and_says_so() -> None:
    async def scenario(bench: Bench) -> None:
        case = await frozen_case(bench)
        await bench.detect("d2", "high")
        stale_case = await bench.case()
        assert (stale_case.stale_rule, stale_case.stale_rules_version) == (
            "severity",
            "stale-rules-1",
        )
        [event] = await announced(bench)
        assert (event.stale_rule, event.evidence_snapshot_id) == (
            "severity",
            case.evidence_snapshot_id,
        )
        # The joining detection wants a new snapshot: re-evaluated, the case is fresh again.
        await bench.builder.run_once(NOW + timedelta(minutes=1))
        fresh = await bench.case()
        assert fresh.evidence_version == 2 and fresh.stale_rule == ""

    with_bench(scenario)


def test_an_alarm_on_the_turbine_makes_its_case_stale_once() -> None:
    async def scenario(bench: Bench) -> None:
        await frozen_case(bench)
        alarm = events_pb2.AlarmChanged(alarm_id="alm_new", turbine_id=TURBINE, code="GBX_HOT",
                                        state=events_pb2.ALARM_STATE_ACTIVE, severity="critical",
                                        version=1)  # fmt: skip
        replayed = Event("alarm.changed", "a-0", TENANT, NOW - timedelta(hours=1), "alarm",
                         alarm, "", "")  # fmt: skip
        assert await stale.on_alarm(bench.engine, replayed) == 0, "from before the snapshot"
        event = Event("alarm.changed", "a-1", TENANT, NOW + timedelta(minutes=1), "alarm", alarm,
                      "", "")  # fmt: skip
        assert await stale.on_alarm(bench.engine, event) == 1
        assert (await bench.case()).stale_rule == "alarm"
        assert await stale.on_alarm(bench.engine, event) == 0, "already stale"
        assert len(await announced(bench)) == 1
        with pytest.raises(InvalidEventError):
            await stale.on_alarm(bench.engine, Event("alarm.changed", "a-2", TENANT, NOW,
                                                     "telemetry", alarm, "", ""))  # fmt: skip

    with_bench(scenario)


def test_the_asset_rule_asks_as_this_service_and_retries_when_it_cannot() -> None:
    async def scenario(bench: Bench) -> None:
        await frozen_case(bench)
        owners = FakeOwners()
        changed = evidence_changed("asset", TURBINE, "4")
        assert await stale.on_evidence(bench.engine, changed, owners, SECRET) == 0, "same"
        owners.down = True
        with pytest.raises(RetryLaterError):
            await stale.on_evidence(bench.engine, changed, owners, SECRET)
        owners.down, owners.status = False, "maintenance"
        assert await stale.on_evidence(bench.engine, changed, owners, SECRET) == 1
        assert (await bench.case()).stale_rule == "asset"
        assert set(owners.callers) == {"service:investigation"}

    with_bench(scenario)


def test_a_proposal_is_watched_for_its_procedures_and_parts() -> None:
    async def scenario(bench: Bench) -> None:
        case = await frozen_case(bench)
        assert await submit(bench, case)
        assert not await submit(bench, case), "a repeat changes nothing"
        owners = FakeOwners()
        assert (
            await stale.on_evidence(
                bench.engine,
                evidence_changed("inventory", "stock:P-1@dogger-east", "7"),
                owners,
                SECRET,
            )
            == 0
        ), "still available"
        assert (
            await stale.on_evidence(
                bench.engine, evidence_changed("knowledge", "YAW-02", "YAW-02@2"), owners, SECRET
            )
            == 0
        ), "not cited"
        owners.stock = "short"
        assert (
            await stale.on_evidence(
                bench.engine,
                evidence_changed("inventory", "stock:P-1@other-farm", "8"),
                owners,
                SECRET,
            )
            == 0
        ), "another store"
        assert (
            await stale.on_evidence(
                bench.engine,
                evidence_changed("inventory", "stock:P-1@dogger-east", "8"),
                owners,
                SECRET,
            )
            == 1
        )
        stale_case = await bench.case()
        assert stale_case.stale_rule == "inventory" and ORDER in stale_case.stale_detail

    with_bench(scenario)


def test_a_replaced_procedure_makes_the_case_stale() -> None:
    async def scenario(bench: Bench) -> None:
        case = await frozen_case(bench)
        await submit(bench, case)
        changed = evidence_changed("knowledge", "GBX-OIL-01", "GBX-OIL-01@4")
        assert await stale.on_evidence(bench.engine, changed, FakeOwners(), SECRET) == 1
        assert (await bench.case()).stale_rule == "procedure"
        forged = evidence_changed("knowledge", "GBX-OIL-01", "GBX-OIL-01@5")
        with pytest.raises(InvalidEventError):
            await stale.on_evidence(bench.engine, Event(
                "evidence.changed", "f", TENANT, NOW, "asset", forged.payload, "", ""),
                FakeOwners(), SECRET)  # fmt: skip

    with_bench(scenario)


def test_old_evidence_goes_stale_by_its_severitys_limit() -> None:
    async def scenario(bench: Bench) -> None:
        await frozen_case(bench)
        assert await stale.sweep(bench.engine, NOW + timedelta(hours=71)) == 0
        assert await stale.sweep(bench.engine, NOW + timedelta(hours=73)) == 1
        assert (await bench.case()).stale_rule == "age"
        assert await stale.sweep(bench.engine, NOW + timedelta(hours=80)) == 0

    with_bench(scenario)


def test_parts_round_trip_through_the_watch_list() -> None:
    parts = [events_pb2.ProposedPart(part_number="GBX/FAN:1", quantity=2, status="short")]
    assert stale.parse_parts(stale.format_parts(parts)) == [("GBX/FAN:1", 2, "short")]
