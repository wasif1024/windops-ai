"""The inputs a proposal is judged on: the proposal itself, and facts gathered from their owners.

The service gathers the facts as itself, with fixed read grants (ADR 0008, ADR 0009), so a caller
can never supply a convenient case severity or evidence age. An owner that cannot be reached
makes its inputs unavailable, which every rule treats as missing: block or escalate (FR-POL-005).
The weather arrives with the Weather Service in M6.3, so until then it is always unavailable.
"""

import json
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from windops.asset.v1 import asset_pb2
from windops.investigation.v1 import investigation_pb2
from windops.policy.v1 import policy_pb2 as pb
from windops.production_impact.v1 import production_impact_pb2
from windops_grpc import InvalidRequestError, NotFoundError, ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

from windops_policy.rules import Input

DEADLINE_SECONDS = 3.0
ACTIONS = ("inspection", "repair", "replacement", "adjustment", "monitoring")
WEATHER_LATER = "The Weather Service arrives in M6.3"


async def ask(client: ServiceClient, method: Any, request: Message, who: str) -> Any:
    try:
        return await client.call(method, request, idempotent=True,
                                 deadline_seconds=DEADLINE_SECONDS)  # fmt: skip
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError(f"{who} has no such record") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


class Sources(Protocol):
    async def case(self, case_id: str) -> Any: ...
    async def evidence(self, snapshot_id: str) -> Any: ...
    async def turbine(self, turbine_id: str) -> Any: ...
    async def impact(self, result_id: str) -> Any: ...


class ServiceSources:
    """Each owner, through its own client and breaker."""

    def __init__(self, stubs: dict[str, Any], clients: dict[str, ServiceClient]) -> None:
        self._stubs, self._clients = stubs, clients

    async def case(self, case_id: str) -> Any:
        request = investigation_pb2.GetCaseRequest(case_id=case_id)
        return (await ask(self._clients["investigation"], self._stubs["investigation"].GetCase,
                          request, "The Investigation Service")).case  # fmt: skip

    async def evidence(self, snapshot_id: str) -> Any:
        request = investigation_pb2.GetEvidenceSnapshotRequest(snapshot_id=snapshot_id)
        stub = self._stubs["investigation"]
        return (await ask(self._clients["investigation"], stub.GetEvidenceSnapshot, request,
                          "The Investigation Service")).snapshot  # fmt: skip

    async def turbine(self, turbine_id: str) -> Any:
        request = asset_pb2.GetTurbineRequest(turbine_id=turbine_id)
        return (await ask(self._clients["asset"], self._stubs["asset"].GetTurbine, request,
                          "The Asset Service")).turbine  # fmt: skip

    async def impact(self, result_id: str) -> Any:
        request = production_impact_pb2.GetImpactResultRequest(result_id=result_id)
        stub = self._stubs["production-impact"]
        return (await ask(self._clients["production-impact"], stub.GetImpactResult, request,
                          "The Production Impact Service")).result  # fmt: skip


def check_proposal(proposal: pb.Proposal) -> None:
    """Refuse a proposal that cannot be judged as stated."""
    if proposal.action not in ACTIONS:
        raise InvalidRequestError(f"proposal.action is one of {', '.join(ACTIONS)}")
    if len(proposal.summary) > 2000:
        raise InvalidRequestError("proposal.summary is at most 2000 characters")
    if proposal.HasField("estimated_cost_eur") and not 0 <= proposal.estimated_cost_eur < 1e12:
        raise InvalidRequestError("proposal.estimated_cost_eur is 0 or more")
    if proposal.HasField("estimated_downtime_hours") and not (
        0 <= proposal.estimated_downtime_hours < 1e6
    ):
        raise InvalidRequestError("proposal.estimated_downtime_hours is 0 or more")
    if proposal.HasField("confidence") and not 0 <= proposal.confidence <= 1:
        raise InvalidRequestError("proposal.confidence is from 0 to 1")
    if proposal.HasField("planned_start") != proposal.HasField("planned_end"):
        raise InvalidRequestError("proposal.planned_start and planned_end come together")
    if proposal.HasField("planned_start") and (
        proposal.planned_end.ToDatetime() <= proposal.planned_start.ToDatetime()
    ):
        raise InvalidRequestError("proposal.planned_end is after planned_start")


def proposal_inputs(proposal: pb.Proposal, now: datetime) -> list[Input]:
    def given(name: str, value: Any) -> Input:
        return Input(f"proposal.{name}", value, "proposal", observed_at=now)

    def optional(name: str, stated: bool, value: float) -> Input:
        if stated:
            return given(name, float(value))
        return Input(f"proposal.{name}", None, "proposal", state="missing", note="not stated")

    found = [given("action", proposal.action), given("requires_climb", proposal.requires_climb)]
    if proposal.HasField("planned_start"):
        span = proposal.planned_end.ToDatetime() - proposal.planned_start.ToDatetime()
        found.append(given("window_hours", span.total_seconds() / 3600))
    else:
        found.append(Input("proposal.window_hours", None, "proposal", state="missing",
                           note="no planned window"))  # fmt: skip
    return [
        *found,
        optional("estimated_cost_eur", proposal.HasField("estimated_cost_eur"),
                 proposal.estimated_cost_eur),
        optional("estimated_downtime_hours", proposal.HasField("estimated_downtime_hours"),
                 proposal.estimated_downtime_hours),
        optional("confidence", proposal.HasField("confidence"), proposal.confidence),
    ]  # fmt: skip


def _unavailable(names: tuple[str, ...], source: str, note: str) -> list[Input]:
    return [Input(name, None, source, state="unavailable", note=note) for name in names]


async def case_inputs(sources: Sources, case: Any, now: datetime) -> list[Input]:
    ref = f"{case.case_id}@{case.version}"
    found = [
        Input("case.status", case.status, "investigation", ref, now),
        Input("case.severity", case.severity, "investigation", ref, now),
        Input("evidence.status", case.evidence_status or "pending", "investigation", ref, now),
    ]
    if not case.evidence_snapshot_id:
        return [*found, Input("evidence.age_hours", None, "investigation", ref, state="missing",
                              note="the case has no frozen evidence yet")]  # fmt: skip
    try:
        snapshot = await sources.evidence(case.evidence_snapshot_id)
    except (UnavailableError, NotFoundError) as error:
        return [*found, *_unavailable(("evidence.age_hours",), "investigation", str(error))]
    frozen = snapshot.created_at.ToDatetime(tzinfo=UTC)
    age = Input("evidence.age_hours", (now - frozen).total_seconds() / 3600, "investigation",
                f"{snapshot.snapshot_id}@{snapshot.version}", frozen)  # fmt: skip
    return [*found, age]


async def turbine_inputs(
    sources: Sources, turbine_id: str, now: datetime
) -> tuple[str, list[Input]]:
    """The turbine's site, or "" when it cannot be read, and its status."""
    try:
        turbine = await sources.turbine(turbine_id)
    except (UnavailableError, NotFoundError) as error:
        return "", _unavailable(("turbine.status",), "asset", str(error))
    ref = f"{turbine.turbine_id}@{getattr(turbine, 'version', 0)}"
    return turbine.wind_farm_id, [Input("turbine.status", turbine.status, "asset", ref, now)]


IMPACT = ("impact.total_cost_eur", "impact.projected_lost_energy_mwh", "impact.downtime_hours")


async def impact_inputs(sources: Sources, result_id: str, case: Any) -> list[Input]:
    """The cost and downtime, from the Production Impact Service's estimate for this case.

    Cost and energy are the high end of the estimate's range, so a limit is judged on the worst
    the estimate allows. An estimate for another case, or one that no longer verifies, is not used.
    """
    if not result_id:
        note = "no Production Impact estimate was given"
        return [Input(name, None, "production-impact", state="missing", note=note)
                for name in IMPACT]  # fmt: skip
    try:
        result = await sources.impact(result_id)
    except (UnavailableError, NotFoundError) as error:
        return _unavailable(IMPACT, "production-impact", str(error))
    if result.case_id != case.case_id:
        return [Input(name, None, "production-impact", result_id, state="missing",
                      note="the estimate is for another case") for name in IMPACT]  # fmt: skip
    if not result.verified:
        return _unavailable(IMPACT, "production-impact", "the estimate's hash does not verify")
    figures = {f.name: f for f in result.figures}
    at = result.estimated_at.ToDatetime(tzinfo=UTC)

    def figure(input_name: str, figure_name: str) -> Input:
        if figure_name not in figures:
            return Input(input_name, None, "production-impact", result_id, at, "missing",
                         f"the estimate has no {figure_name}")  # fmt: skip
        return Input(input_name, figures[figure_name].high, "production-impact", result_id, at)

    return [
        figure("impact.total_cost_eur", "total_cost_eur"),
        figure("impact.projected_lost_energy_mwh", "projected_lost_energy_mwh"),
        Input("impact.downtime_hours", float(result.downtime_hours), "production-impact",
              result_id, at),
    ]  # fmt: skip


def weather_inputs() -> list[Input]:
    return _unavailable(("weather.max_wind_speed_mps", "weather.max_gust_mps"), "weather",
                        WEATHER_LATER)  # fmt: skip


def to_input(item: Input) -> pb.PolicyInput:
    observed = None
    if item.observed_at is not None:
        observed = Timestamp()
        observed.FromDatetime(item.observed_at)
    return pb.PolicyInput(
        name=item.name, value_json="" if item.value is None else json.dumps(item.value),
        source=item.source, source_ref=item.source_ref, observed_at=observed, state=item.state,
        note=item.note,
    )  # fmt: skip
