"""WindOps MCP's tools: the platform's own records, for agents, in the caller's tenant (SRS 6.3).

Every tool calls a domain service as the verified caller. The tenant comes from the token, never
from a tool's input, and each service decides what the caller, and the agent acting for them, may
read (SEC-009). Outputs carry only what an agent needs: no user's ID, and no field a service adds
later reaches an agent unless it is named here.

| Tool | Service | What it does |
| --- | --- | --- |
| get_turbine | Asset | A turbine's model, rating and status |
| get_latest_telemetry | Telemetry | Its latest reading of each metric, with its quality |
| get_telemetry_summary | Telemetry | A frozen snapshot's readings, summarized per metric |
| list_alarms | Alarm | Its alarms, newest first |
| get_case | Investigation | A case |
| list_cases | Investigation | A turbine's cases: its fault history |
| get_evidence | Investigation | A frozen evidence snapshot, item by item |
| estimate_impact | Production Impact | A deterministic estimate: energy, availability and cost |
| check_policy | Policy | Deterministic rules on a proposed action |
| create_draft_work_order | Maintenance | A draft work order: the one change an agent may make |

Nothing here changes a record. `estimate_impact` and `check_policy` each keep the result they
compute, as their services always do, and change nothing else. Parts and drafts come with the
Inventory and Maintenance services (Phase 9).
"""

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import Field
from windops.alarm.v1 import alarm_pb2, alarm_pb2_grpc
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.investigation.v1 import investigation_pb2, investigation_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2, maintenance_pb2_grpc
from windops.policy.v1 import policy_pb2, policy_pb2_grpc
from windops.production_impact.v1 import production_impact_pb2, production_impact_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2, telemetry_pb2_grpc
from windops_mcp import Tool, ToolInput, ToolOutput, current_caller
from windops_mcp.serve import DEADLINE_SECONDS, Services

SERVER = "windops"
IMPACT_DEADLINE_SECONDS = 15.0
INSTRUCTIONS = (
    "Read the tenant's turbines, telemetry, alarms, cases and frozen evidence, and get "
    "deterministic impact estimates and policy checks. Cite records by their IDs. Every figure "
    "here comes from a service: never invent or change one."
)
_ID = r"^[A-Za-z0-9_.:-]+$"
_CASE = r"^case_[a-z0-9]{20}$"
_CONTENT_LIMIT = 4000
ACTIONS = ("inspection", "repair", "replacement", "adjustment", "monitoring")
Action = Literal["inspection", "repair", "replacement", "adjustment", "monitoring"]


def _iso(stamp: Timestamp) -> str:
    if not stamp.seconds and not stamp.nanos:
        return ""
    return stamp.ToDatetime(tzinfo=UTC).isoformat()


def _clip(text: str) -> str:
    return text if len(text) <= _CONTENT_LIMIT else text[:_CONTENT_LIMIT] + "…"


def _key(tool: str, *parts: object) -> str:
    """The same call by the same caller is one request, so a retry keeps one result (GRP-007)."""
    caller = current_caller()
    material = "|".join([tool, caller.tenant_id, caller.user_id, caller.agent, *map(str, parts)])
    return f"mcp-{tool}-{hashlib.sha256(material.encode()).hexdigest()[:40]}"


# --- Inputs


class TurbineInput(ToolInput):
    turbine_id: str = Field(min_length=1, max_length=64, pattern=_ID)


class AlarmsInput(ToolInput):
    turbine_id: str = Field(min_length=1, max_length=64, pattern=_ID)
    include_cleared: bool = Field(default=False, description="Also return cleared alarms")
    limit: int = Field(default=20, ge=1, le=100)


class CaseInput(ToolInput):
    case_id: str = Field(max_length=25, pattern=_CASE)


class CasesInput(ToolInput):
    turbine_id: str = Field(min_length=1, max_length=64, pattern=_ID)
    include_closed: bool = Field(default=True, description="Also return closed and cancelled cases")
    limit: int = Field(default=20, ge=1, le=100)


class EvidenceInput(ToolInput):
    snapshot_id: str = Field(max_length=24, pattern=r"^evs_[a-z0-9]{20}$")
    limit: int = Field(default=50, ge=1, le=200, description="Items to return, in order")


# The metrics an operational sample carries, by the names agents see.
MetricName = Literal[
    "gearbox_temperature", "bearing_temperature", "vibration_rms", "wind_speed", "wind_direction",
    "rotor_speed", "generator_speed", "power", "pitch_angle", "ambient_temperature",
    "main_bearing_temperature", "generator_bearing_temperature",
]  # fmt: skip


class SummaryInput(ToolInput):
    snapshot_id: str = Field(
        max_length=26, pattern=r"^tsnap_[a-z0-9]{20}$",
        description="A telemetry snapshot's ID, as an evidence item's record_id gives it",
    )  # fmt: skip
    metrics: list[MetricName] = Field(default_factory=list, max_length=12,
                                      description="Empty for every metric")  # fmt: skip
    buckets: int = Field(default=12, ge=1, le=48, description="Equal slices of the window")


class DraftTask(ToolInput):
    description: str = Field(min_length=1, max_length=2000)
    duration_hours: float = Field(gt=0, le=240)
    crew_size: int = Field(ge=1, le=20)
    requires_climb: bool


class DraftPart(ToolInput):
    part_number: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]+$")
    quantity: int = Field(ge=1, le=1000)


# A knowledge passage's citation, as the Documentation server gives it.
Citation = Annotated[
    str, Field(max_length=90, pattern=r"^kn:[A-Z0-9][A-Z0-9-]{2,63}@\d{1,4}#\d{1,5}$")
]


class DraftInput(ToolInput):
    case_id: str = Field(max_length=25, pattern=_CASE)
    title: str = Field(min_length=1, max_length=200)
    action: Action
    summary: str = Field(min_length=1, max_length=4000)
    tasks: list[DraftTask] = Field(min_length=1, max_length=50)
    parts: list[DraftPart] = Field(default_factory=list, max_length=50)
    procedures: list[Citation] = Field(
        default_factory=list,
        max_length=20,
        description="Approved procedures by citation, as documentation_read_passage gave them",
    )
    planned_start: datetime
    planned_end: datetime
    estimated_downtime_hours: float = Field(ge=0, le=720)
    impact_result_id: str = Field(default="", max_length=40, pattern=r"^(imp_[a-z0-9]{20})?$")


class ImpactInput(ToolInput):
    case_id: str = Field(max_length=25, pattern=_CASE)
    action: Action
    downtime_hours: float = Field(gt=0, le=720)


class PolicyInput(ToolInput):
    case_id: str = Field(max_length=25, pattern=_CASE)
    action: Action
    summary: str = Field(min_length=1, max_length=500)
    requires_climb: bool
    planned_start: datetime | None = None
    planned_end: datetime | None = None
    impact_result_id: str = Field(
        default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$",
        description="estimate_impact's result for this case and action",
    )  # fmt: skip


# --- Outputs


class Turbine(ToolOutput):
    turbine_id: str
    wind_farm_id: str
    name: str
    manufacturer: str
    model: str
    rated_power_kw: float
    rotor_diameter_m: float
    hub_height_m: float
    status: str
    status_reason: str
    status_changed_at: str


class Point(ToolOutput):
    metric: str
    value: float
    unit: str
    quality: str
    event_time: str


class Telemetry(ToolOutput):
    turbine_id: str
    event_time: str
    operating_state: str
    available: bool
    points: list[Point]


class Alarm(ToolOutput):
    alarm_id: str
    code: str
    tag: str
    description: str
    severity: str
    state: str
    raised_at: str
    cleared_at: str
    repeat_count: int


class Alarms(ToolOutput):
    alarms: list[Alarm]


class Case(ToolOutput):
    case_id: str
    turbine_id: str
    component_group: str
    title: str
    status: str
    severity: str
    origin: str
    created_at: str
    last_detection_at: str
    detection_count: int
    evidence_snapshot_id: str
    evidence_status: str


class OneCase(ToolOutput):
    case: Case


class Cases(ToolOutput):
    cases: list[Case]


class SummaryBucket(ToolOutput):
    start: str
    end: str
    readings: int
    mean: float | None


class MetricSummary(ToolOutput):
    # Cite a figure by this: the snapshot and the metric.
    record_id: str
    metric: str
    unit: str
    readings: int
    good: int
    suspect: int
    missing: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    first: float | None
    last: float | None
    first_at: str
    last_at: str
    trend_per_hour: float | None
    buckets: list[SummaryBucket]


class TelemetrySummary(ToolOutput):
    snapshot_id: str
    turbine_id: str
    window_start: str
    window_end: str
    content_hash: str
    metrics: list[MetricSummary]


class DraftProblem(ToolOutput):
    field: str
    code: str
    message: str
    blocking: bool


class DraftWorkOrder(ToolOutput):
    work_order_id: str
    status: str
    revision: int
    case_id: str
    drafted_by_agent: str
    passed: bool
    problems: list[DraftProblem]
    policy_outcome: str


class EvidenceItem(ToolOutput):
    position: int
    source_service: str
    record_type: str
    record_id: str
    version: str
    window_start: str
    window_end: str
    data_quality_json: str
    content_json: str
    content_hash: str


class Evidence(ToolOutput):
    snapshot_id: str
    case_id: str
    turbine_id: str
    version: int
    content_hash: str
    verified: bool
    window_start: str
    window_end: str
    item_count: int
    items: list[EvidenceItem]


class Figure(ToolOutput):
    name: str
    value: float
    low: float
    high: float
    unit: str


class Impact(ToolOutput):
    result_id: str
    case_id: str
    turbine_id: str
    action: str
    downtime_hours: float
    formula_version: str
    figures: list[Figure]
    assumptions: list[str]
    content_hash: str
    verified: bool


class Rule(ToolOutput):
    rule_id: str
    version: int
    category: str
    outcome: str
    reason: str


class PolicyCheck(ToolOutput):
    result_id: str
    case_id: str
    outcome: str
    rules: list[Rule]
    content_hash: str
    verified: bool


def _case(found: Any) -> Case:
    return Case(
        case_id=found.case_id, turbine_id=found.turbine_id,
        component_group=found.component_group, title=found.title, status=found.status,
        severity=found.severity, origin=found.origin, created_at=_iso(found.created_at),
        last_detection_at=_iso(found.last_detection_at),
        detection_count=found.detection_count,
        evidence_snapshot_id=found.evidence_snapshot_id, evidence_status=found.evidence_status,
    )  # fmt: skip


def _enum(name: str, prefix: str) -> str:
    return name.removeprefix(prefix).lower()


def tools(services: Services) -> list[Tool]:
    async def call(dependency: str, stub: Any, setting: str, method: str, request: Any,
                   deadline_seconds: float = DEADLINE_SECONDS) -> Any:  # fmt: skip
        return await services.call(dependency, stub, setting, method, request,
                                   deadline_seconds=deadline_seconds)  # fmt: skip

    async def get_turbine(arguments: TurbineInput) -> Turbine:
        found = (
            await call("asset", asset_pb2_grpc.AssetServiceStub, "WINDOPS_ASSET_ADDRESS",
                       "GetTurbine", asset_pb2.GetTurbineRequest(turbine_id=arguments.turbine_id))
        ).turbine  # fmt: skip
        return Turbine(
            turbine_id=found.turbine_id, wind_farm_id=found.wind_farm_id, name=found.name,
            manufacturer=found.model.manufacturer, model=found.model.model,
            rated_power_kw=found.model.rated_power_kw,
            rotor_diameter_m=found.model.rotor_diameter_m, hub_height_m=found.model.hub_height_m,
            status=found.status, status_reason=found.status_reason,
            status_changed_at=_iso(found.status_changed_at),
        )  # fmt: skip

    async def get_latest_telemetry(arguments: TurbineInput) -> Telemetry:
        answer = await call(
            "telemetry", telemetry_pb2_grpc.TelemetryServiceStub, "WINDOPS_TELEMETRY_ADDRESS",
            "GetLatestTelemetry",
            telemetry_pb2.GetLatestTelemetryRequest(turbine_ids=[arguments.turbine_id]),
        )  # fmt: skip
        if not answer.turbines:
            return Telemetry(turbine_id=arguments.turbine_id, event_time="",
                             operating_state="", available=False, points=[])  # fmt: skip
        latest = answer.turbines[0]
        return Telemetry(
            turbine_id=latest.turbine_id, event_time=_iso(latest.event_time),
            operating_state=latest.operating_state, available=latest.available,
            points=[
                Point(
                    metric=_enum(telemetry_pb2.Metric.Name(p.metric), "METRIC_"), value=p.value,
                    unit=p.unit,
                    quality=p.quality_label
                    or _enum(telemetry_pb2.Quality.Name(p.quality), "QUALITY_"),
                    event_time=_iso(p.event_time),
                )
                for p in latest.points
            ],
        )  # fmt: skip

    async def list_alarms(arguments: AlarmsInput) -> Alarms:
        states = [] if not arguments.include_cleared else [
            alarm_pb2.ALARM_STATE_ACTIVE, alarm_pb2.ALARM_STATE_ACKNOWLEDGED,
            alarm_pb2.ALARM_STATE_CLEARED, alarm_pb2.ALARM_STATE_SHELVED,
        ]  # fmt: skip
        answer = await call(
            "alarm",
            alarm_pb2_grpc.AlarmServiceStub,
            "WINDOPS_ALARM_ADDRESS",
            "ListAlarms",
            alarm_pb2.ListAlarmsRequest(
                turbine_id=arguments.turbine_id, states=states, page_size=arguments.limit
            ),
        )
        return Alarms(
            alarms=[
                Alarm(
                    alarm_id=a.alarm_id, code=a.code, tag=a.tag, description=a.description,
                    severity=_enum(alarm_pb2.AlarmSeverity.Name(a.severity), "ALARM_SEVERITY_"),
                    state=_enum(alarm_pb2.AlarmState.Name(a.state), "ALARM_STATE_"),
                    raised_at=_iso(a.raised_at), cleared_at=_iso(a.cleared_at),
                    repeat_count=a.repeat_count,
                )
                for a in answer.alarms[: arguments.limit]
            ]
        )  # fmt: skip

    async def investigation(method: str, request: Any) -> Any:
        return await call(
            "investigation", investigation_pb2_grpc.InvestigationServiceStub,
            "WINDOPS_INVESTIGATION_ADDRESS", method, request,
        )  # fmt: skip

    async def get_case(arguments: CaseInput) -> OneCase:
        answer = await investigation(
            "GetCase", investigation_pb2.GetCaseRequest(case_id=arguments.case_id)
        )
        return OneCase(case=_case(answer.case))

    async def list_cases(arguments: CasesInput) -> Cases:
        statuses = (
            ["open", "investigating", "pending_review", "closed", "cancelled"]
            if arguments.include_closed
            else []
        )
        answer = await investigation(
            "ListCases",
            investigation_pb2.ListCasesRequest(
                turbine_id=arguments.turbine_id, statuses=statuses, page_size=arguments.limit
            ),
        )
        return Cases(cases=[_case(c) for c in answer.cases[: arguments.limit]])

    async def get_evidence(arguments: EvidenceInput) -> Evidence:
        snapshot = (
            await investigation(
                "GetEvidenceSnapshot",
                investigation_pb2.GetEvidenceSnapshotRequest(snapshot_id=arguments.snapshot_id),
            )
        ).snapshot
        return Evidence(
            snapshot_id=snapshot.snapshot_id, case_id=snapshot.case_id,
            turbine_id=snapshot.turbine_id, version=snapshot.version,
            content_hash=snapshot.content_hash, verified=snapshot.verified,
            window_start=_iso(snapshot.window_start), window_end=_iso(snapshot.window_end),
            item_count=snapshot.item_count,
            items=[
                EvidenceItem(
                    position=index, source_service=item.source_service,
                    record_type=item.record_type, record_id=item.record_id, version=item.version,
                    window_start=_iso(item.window_start), window_end=_iso(item.window_end),
                    data_quality_json=_clip(item.data_quality_json),
                    content_json=_clip(item.content_json), content_hash=item.content_hash,
                )
                for index, item in enumerate(snapshot.items[: arguments.limit], start=1)
            ],
        )  # fmt: skip

    async def get_telemetry_summary(arguments: SummaryInput) -> TelemetrySummary:
        def value(message: Any, name: str) -> float | None:
            return float(getattr(message, name)) if message.HasField(name) else None

        answer = await call(
            "telemetry", telemetry_pb2_grpc.TelemetryServiceStub, "WINDOPS_TELEMETRY_ADDRESS",
            "SummarizeTelemetrySnapshot",
            telemetry_pb2.SummarizeTelemetrySnapshotRequest(
                snapshot_id=arguments.snapshot_id, buckets=arguments.buckets,
                metrics=[telemetry_pb2.Metric.Value(f"METRIC_{m.upper()}")
                         for m in arguments.metrics],
            ),
        )  # fmt: skip
        snapshot = answer.snapshot
        metrics = []
        for m in answer.metrics:
            name = _enum(telemetry_pb2.Metric.Name(m.metric), "METRIC_")
            metrics.append(MetricSummary(
                record_id=f"{snapshot.snapshot_id}#{name}", metric=name, unit=m.unit,
                readings=m.readings, good=m.good, suspect=m.suspect, missing=m.missing,
                minimum=value(m, "minimum"), maximum=value(m, "maximum"), mean=value(m, "mean"),
                first=value(m, "first"), last=value(m, "last"), first_at=_iso(m.first_at),
                last_at=_iso(m.last_at), trend_per_hour=value(m, "trend_per_hour"),
                buckets=[SummaryBucket(start=_iso(b.start), end=_iso(b.end), readings=b.readings,
                                       mean=value(b, "mean")) for b in m.buckets],
            ))  # fmt: skip
        return TelemetrySummary(
            snapshot_id=snapshot.snapshot_id, turbine_id=snapshot.turbine_id,
            window_start=_iso(snapshot.from_time), window_end=_iso(snapshot.to_time),
            content_hash=snapshot.content_hash, metrics=metrics,
        )  # fmt: skip

    async def estimate_impact(arguments: ImpactInput) -> Impact:
        result = (
            await call(
                "production-impact", production_impact_pb2_grpc.ProductionImpactServiceStub,
                "WINDOPS_PRODUCTION_IMPACT_ADDRESS", "EstimateImpact",
                production_impact_pb2.EstimateImpactRequest(
                    case_id=arguments.case_id, action=arguments.action,
                    downtime_hours=arguments.downtime_hours,
                    idempotency_key=_key("impact", arguments.case_id, arguments.action,
                                         arguments.downtime_hours),
                ),
                # An estimate reads the case, the turbine and a forecast: about 7 seconds,
                # inside the tool's own 20.
                deadline_seconds=IMPACT_DEADLINE_SECONDS,
            )
        ).result  # fmt: skip
        return Impact(
            result_id=result.result_id, case_id=result.case_id, turbine_id=result.turbine_id,
            action=result.action, downtime_hours=result.downtime_hours,
            formula_version=result.formula_version,
            figures=[Figure(name=f.name, value=f.value, low=f.low, high=f.high, unit=f.unit)
                     for f in result.figures],
            assumptions=list(result.assumptions), content_hash=result.content_hash,
            verified=result.verified,
        )  # fmt: skip

    async def create_draft_work_order(arguments: DraftInput) -> DraftWorkOrder:
        def stamp(value: datetime) -> Timestamp:
            made = Timestamp()
            made.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
            return made

        plan = maintenance_pb2.Plan(
            title=arguments.title, action=arguments.action, summary=arguments.summary,
            tasks=[maintenance_pb2.Task(description=t.description, duration_hours=t.duration_hours,
                                        crew_size=t.crew_size, requires_climb=t.requires_climb)
                   for t in arguments.tasks],
            parts=[maintenance_pb2.PartLine(part_number=p.part_number, quantity=p.quantity)
                   for p in arguments.parts],
            procedures=arguments.procedures, planned_start=stamp(arguments.planned_start),
            planned_end=stamp(arguments.planned_end),
            estimated_downtime_hours=arguments.estimated_downtime_hours,
            impact_result_id=arguments.impact_result_id,
        )  # fmt: skip
        order = (
            await call(
                "maintenance", maintenance_pb2_grpc.MaintenanceServiceStub,
                "WINDOPS_MAINTENANCE_ADDRESS", "CreateDraft",
                maintenance_pb2.CreateDraftRequest(
                    case_id=arguments.case_id, plan=plan,
                    idempotency_key=_key("draft", arguments.model_dump_json()),
                ),
                # A draft is validated at once, through the Policy Service.
                deadline_seconds=IMPACT_DEADLINE_SECONDS,
            )
        ).work_order  # fmt: skip
        return DraftWorkOrder(
            work_order_id=order.work_order_id, status=order.status, revision=order.revision,
            case_id=order.case_id, drafted_by_agent=order.drafted_by_agent,
            passed=order.validation.passed,
            problems=[DraftProblem(field=p.field, code=p.code, message=p.message,
                                   blocking=p.blocking) for p in order.validation.problems],
            policy_outcome=order.validation.policy_outcome,
        )  # fmt: skip

    async def check_policy(arguments: PolicyInput) -> PolicyCheck:
        proposal = policy_pb2.Proposal(
            action=arguments.action, summary=arguments.summary,
            requires_climb=arguments.requires_climb,
        )  # fmt: skip
        if arguments.planned_start is not None:
            proposal.planned_start.FromDatetime(arguments.planned_start.astimezone(UTC))
        if arguments.planned_end is not None:
            proposal.planned_end.FromDatetime(arguments.planned_end.astimezone(UTC))
        result = (
            await call(
                "policy", policy_pb2_grpc.PolicyServiceStub, "WINDOPS_POLICY_ADDRESS",
                "EvaluateProposal",
                policy_pb2.EvaluateProposalRequest(
                    case_id=arguments.case_id, proposal=proposal,
                    impact_result_id=arguments.impact_result_id,
                    idempotency_key=_key("policy", arguments.model_dump_json()),
                ),
            )
        ).result  # fmt: skip
        return PolicyCheck(
            result_id=result.result_id, case_id=result.case_id, outcome=result.outcome,
            rules=[Rule(rule_id=r.rule_id, version=r.version, category=r.category,
                        outcome=r.outcome, reason=r.reason) for r in result.rules],
            content_hash=result.content_hash, verified=result.verified,
        )  # fmt: skip

    def tool(name: str, title: str, description: str, input_: type[ToolInput],
             output: type[ToolOutput], handler: Any, **limits: Any) -> Tool:  # fmt: skip
        return Tool(
            server=SERVER, name=name, title=title, description=description, input=input_,
            output=output, handler=handler, **limits,
        )  # fmt: skip

    return [
        tool("get_turbine", "Get a turbine",
             "A turbine's wind farm, model, rated power, rotor, hub height and status.",
             TurbineInput, Turbine, get_turbine, timeout_seconds=5, per_minute=60),
        tool("get_latest_telemetry", "Get the latest telemetry",
             "A turbine's latest reading of each metric, each with its unit and quality label. "
             "A reading marked suspect or missing is not evidence on its own.",
             TurbineInput, Telemetry, get_latest_telemetry, timeout_seconds=5, per_minute=60),
        tool("get_telemetry_summary", "Summarize frozen telemetry",
             "A frozen telemetry snapshot's readings, summarized per metric: their count and "
             "quality, minimum, maximum, mean, first and last, the trend per hour, and the mean "
             "of each slice of the window. Take the snapshot_id from the telemetry item of the "
             "case's evidence. Cite a figure by its metric's record_id. A missing reading is "
             "counted, never averaged; suspect readings are not evidence on their own.",
             SummaryInput, TelemetrySummary, get_telemetry_summary, timeout_seconds=20,
             per_minute=20),
        tool("list_alarms", "List a turbine's alarms",
             "A turbine's alarms, newest first, each with its code, severity and state.",
             AlarmsInput, Alarms, list_alarms, timeout_seconds=5, per_minute=60),
        tool("get_case", "Get a case", "A case: its turbine, component group, status, "
             "severity, detections and current evidence snapshot.",
             CaseInput, OneCase, get_case, timeout_seconds=5, per_minute=60),
        tool("list_cases", "List a turbine's cases",
             "A turbine's cases, newest first: its fault history.",
             CasesInput, Cases, list_cases, timeout_seconds=5, per_minute=30),
        tool("get_evidence", "Get an evidence snapshot",
             "A frozen evidence snapshot and its items: what each record was, from which "
             "service, its window, data quality and content hash. Cite items by record_id.",
             EvidenceInput, Evidence, get_evidence, timeout_seconds=10, per_minute=30),
        tool("estimate_impact", "Estimate the production impact",
             "A deterministic estimate of lost energy, availability and cost for an action on "
             "a case, each figure with a low to high range and its assumptions. Explain these "
             "figures; never change or invent one (FR-IMP-004).",
             ImpactInput, Impact, estimate_impact, timeout_seconds=20, per_minute=10, per_day=500),
        tool("check_policy", "Check a proposal against policy",
             "The Policy Service's deterministic rules on a proposed action: pass, escalate "
             "or block, each rule with its version and reason. An agent cannot override it.",
             PolicyInput, PolicyCheck, check_policy, timeout_seconds=20, per_minute=10,
             per_day=500),
        tool("create_draft_work_order", "Draft a work order",
             "A draft work order for a case: its tasks, crew, window, parts, the procedures it "
             "follows and the expected downtime. It is validated at once, and its problems come "
             "back. Only a draft: a person submits it, and another decides (FR-MNT-003).",
             DraftInput, DraftWorkOrder, create_draft_work_order, timeout_seconds=20,
             per_minute=10, per_day=200, read_only=False),
    ]  # fmt: skip
