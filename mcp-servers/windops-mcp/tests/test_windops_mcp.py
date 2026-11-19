"""WindOps MCP: each tool calls its service as the caller, and returns only what it names."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import ValidationError
from windops.alarm.v1 import alarm_pb2
from windops.asset.v1 import asset_pb2
from windops.investigation.v1 import investigation_pb2
from windops.policy.v1 import policy_pb2
from windops.production_impact.v1 import production_impact_pb2
from windops.telemetry.v1 import telemetry_pb2
from windops_mcp import ToolCaller, caller_scope
from windops_mcp_windops import tools as module
from windops_mcp_windops.tools import (
    AlarmsInput,
    CaseInput,
    CasesInput,
    EvidenceInput,
    ImpactInput,
    PolicyInput,
    TurbineInput,
)

CASE = "case_" + "a" * 20
SNAPSHOT = "evs_" + "b" * 20
STAMP = Timestamp(seconds=1_795_000_000)


def a_case(case_id: str = CASE) -> Any:
    return investigation_pb2.Case(
        case_id=case_id, turbine_id="dogger-east-t01", component_group="gearbox",
        title="Gearbox: GBX-TEMP", status="open", severity="high", origin="detection",
        created_by="u-operator", created_at=STAMP, detection_count=2,
        evidence_snapshot_id=SNAPSHOT, evidence_status="frozen",
    )  # fmt: skip


class Fake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def call(self, dependency: str, make: Any, setting: str, method: str, request: Any,
                   **_: Any) -> Any:  # fmt: skip
        self.calls.append((dependency, method, request))
        answers: dict[str, Any] = {
            "GetTurbine": asset_pb2.GetTurbineResponse(turbine=asset_pb2.Turbine(
                turbine_id="dogger-east-t01", wind_farm_id="dogger-east", name="Dogger East T01",
                model=asset_pb2.TurbineModel(manufacturer="Synthetic", model="WS-8.0-167",
                                             rated_power_kw=8000, rotor_diameter_m=167,
                                             hub_height_m=110),
                serial_number="SYN-123", status="operating", status_changed_at=STAMP,
            )),
            "GetLatestTelemetry": telemetry_pb2.GetLatestTelemetryResponse(turbines=[
                telemetry_pb2.LatestTelemetry(
                    turbine_id="dogger-east-t01", event_time=STAMP, operating_state="running",
                    available=True,
                    points=[telemetry_pb2.TelemetryPoint(
                        metric=telemetry_pb2.METRIC_GEARBOX_TEMPERATURE, value=88.5, unit="degC",
                        quality=telemetry_pb2.QUALITY_SUSPECT, event_time=STAMP,
                    )],
                )
            ]),
            "ListAlarms": alarm_pb2.ListAlarmsResponse(alarms=[alarm_pb2.Alarm(
                alarm_id="alm_1", turbine_id="dogger-east-t01", code="GBX-TEMP",
                description="Gearbox oil temperature high",
                severity=alarm_pb2.ALARM_SEVERITY_HIGH, state=alarm_pb2.ALARM_STATE_ACTIVE,
                raised_at=STAMP, acknowledged_by="u-operator", repeat_count=3,
            )]),
            "GetCase": investigation_pb2.GetCaseResponse(case=a_case()),
            "ListCases": investigation_pb2.ListCasesResponse(cases=[a_case(), a_case()]),
            "GetEvidenceSnapshot": investigation_pb2.GetEvidenceSnapshotResponse(
                snapshot=investigation_pb2.EvidenceSnapshot(
                    snapshot_id=SNAPSHOT, case_id=CASE, turbine_id="dogger-east-t01", version=1,
                    content_hash="h", verified=True, item_count=2,
                    items=[investigation_pb2.EvidenceItem(
                        source_service="telemetry", record_type="sample", record_id=f"tel_{n}",
                        version="1", content_json="{" + "x" * 5000 + "}", content_hash="c",
                    ) for n in range(2)],
                )
            ),
            "EstimateImpact": production_impact_pb2.EstimateImpactResponse(
                result=production_impact_pb2.ImpactResult(
                    result_id="imp_1", case_id=CASE, action="repair", downtime_hours=12,
                    formula_version="1",
                    figures=[production_impact_pb2.Figure(name="lost_energy", value=40,
                                                          low=30, high=50, unit="MWh")],
                    assumptions=["Observed wind held steady"], content_hash="i", verified=True,
                    estimated_by="u-operator",
                )
            ),
            "EvaluateProposal": policy_pb2.EvaluateProposalResponse(result=policy_pb2.PolicyResult(
                result_id="pol_1", case_id=CASE, outcome="escalate",
                rules=[policy_pb2.RuleOutcome(rule_id="climb-wind", version=2,
                                              category="safety", outcome="escalate",
                                              reason="Wind forecast missing")],
                content_hash="p", verified=True, evaluated_by="u-operator",
            )),
        }  # fmt: skip
        return answers[method]


def by_name(fake: Fake) -> dict[str, Any]:
    return {tool.name: tool for tool in module.tools(fake)}  # type: ignore[arg-type]


@contextmanager
def as_agent(agent: str = "agent:production-impact") -> Iterator[None]:
    caller = ToolCaller(
        user_id="u-operator", tenant_id="north-sea", scopes=frozenset(), expires_at=2**40,
        agent=agent,
    )  # fmt: skip
    with caller_scope(caller):
        yield


def run(fake: Fake, name: str, arguments: Any) -> Any:
    with as_agent():
        return asyncio.run(by_name(fake)[name].handler(arguments))


def test_every_tool_only_reads_and_returns_nothing_external() -> None:
    """MCP-006: so any agent may hold any of them alongside external content."""
    for tool in module.tools(Fake()):  # type: ignore[arg-type]
        assert tool.read_only and not tool.external, tool.name
        assert tool.server == "windops"


def test_turbine_and_telemetry_name_only_what_an_agent_needs() -> None:
    fake = Fake()
    turbine = run(fake, "get_turbine", TurbineInput(turbine_id="dogger-east-t01"))
    assert (turbine.model, turbine.rated_power_kw, turbine.status) == (
        "WS-8.0-167",
        8000,
        "operating",
    )
    assert "serial_number" not in turbine.model_dump()
    telemetry = run(fake, "get_latest_telemetry", TurbineInput(turbine_id="dogger-east-t01"))
    point = telemetry.points[0]
    assert (point.metric, point.value, point.quality) == ("gearbox_temperature", 88.5, "suspect")
    assert fake.calls[1][2].turbine_ids == ["dogger-east-t01"]


def test_alarms_leave_out_who_acknowledged_them() -> None:
    fake = Fake()
    alarms = run(fake, "list_alarms", AlarmsInput(turbine_id="dogger-east-t01", limit=5))
    alarm = alarms.alarms[0]
    assert (alarm.code, alarm.severity, alarm.state, alarm.repeat_count) == (
        "GBX-TEMP", "high", "active", 3,
    )  # fmt: skip
    assert "u-operator" not in alarms.model_dump_json()
    assert list(fake.calls[0][2].states) == [] and fake.calls[0][2].page_size == 5


def test_cases_and_evidence_are_cited_and_clipped() -> None:
    fake = Fake()
    case = run(fake, "get_case", CaseInput(case_id=CASE)).case
    assert case.evidence_snapshot_id == SNAPSHOT and "u-operator" not in case.model_dump_json()
    history = run(fake, "list_cases", CasesInput(turbine_id="dogger-east-t01", limit=1))
    assert len(history.cases) == 1 and "closed" in list(fake.calls[1][2].statuses)
    evidence = run(fake, "get_evidence", EvidenceInput(snapshot_id=SNAPSHOT, limit=1))
    assert [i.record_id for i in evidence.items] == ["tel_0"] and evidence.item_count == 2
    assert len(evidence.items[0].content_json) == 4001


def test_impact_and_policy_are_the_services_own_and_a_retry_is_one_request() -> None:
    fake = Fake()
    asked = ImpactInput(case_id=CASE, action="repair", downtime_hours=12)
    impact = run(fake, "estimate_impact", asked)
    run(fake, "estimate_impact", asked)
    assert impact.figures[0].low == 30 and impact.assumptions == ["Observed wind held steady"]
    keys = [request.idempotency_key for _, _, request in fake.calls]
    assert keys[0] == keys[1] and keys[0].startswith("mcp-impact-")
    with as_agent("agent:supervisor"):
        other = asyncio.run(by_name(fake)["estimate_impact"].handler(asked))
    assert other.result_id == "imp_1" and fake.calls[-1][2].idempotency_key != keys[0]
    policy = run(fake, "check_policy", PolicyInput(
        case_id=CASE, action="repair", summary="Replace the gearbox oil filter",
        requires_climb=True, impact_result_id="imp_1",
    ))  # fmt: skip
    assert policy.outcome == "escalate" and policy.rules[0].reason == "Wind forecast missing"
    assert fake.calls[-1][2].proposal.requires_climb is True


@pytest.mark.parametrize(
    ("model", "arguments"),
    [
        (CaseInput, {"case_id": "case_1"}),
        (CaseInput, {"case_id": CASE, "tenant_id": "atlas-wind"}),
        (ImpactInput, {"case_id": CASE, "action": "approve", "downtime_hours": 1}),
        (ImpactInput, {"case_id": CASE, "action": "repair", "downtime_hours": 721}),
        (AlarmsInput, {"turbine_id": "t1", "limit": 101}),
        (
            PolicyInput,
            {"case_id": CASE, "action": "repair", "summary": "", "requires_climb": False},
        ),
    ],
)
def test_inputs_are_checked_and_never_take_a_tenant(model: Any, arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(arguments)
