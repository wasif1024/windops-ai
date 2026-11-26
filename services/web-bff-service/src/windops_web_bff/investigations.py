"""Investigations: started with the case's ID at once, then followed live over SSE (M8.11, API-003).

It decides nothing. The Investigation Service checks who may investigate a turbine and read a
case, starts the run once the case's evidence is frozen, and keeps each step as a numbered event.
This module passes those events on as server-sent events, each with its sequence as its ID, so a
client that reconnects with `Last-Event-ID` carries on after the last it saw.

A browser's `EventSource` cannot send an `Authorization` header, so the frontend reads the stream
with `fetch`, which can. A token is never put in a URL.
"""

import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

import grpc
from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from windops.investigation.v1 import investigation_pb2 as pb

from windops_web_bff.common import ID_PATTERN, Call, Key, time_of
from windops_web_bff.tokens import Caller, bearer

CaseId = Annotated[str, Path(pattern=ID_PATTERN)]
# Every status a case can have, for a list of them all.
STATUSES = (
    "open", "investigating", "pending_review", "approved", "verifying", "verified", "closed",
    "cancelled",
)  # fmt: skip
# The longest one stream lasts. The Investigation Service ends its own watch sooner, and a
# client reconnects with Last-Event-ID either way.
STREAM_SECONDS = 300
# Tells a client how soon to reconnect after a stream ends without "finished".
RETRY_MILLISECONDS = 3000
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # The gateway's nginx passes each event on as it comes, rather than buffering it.
    "X-Accel-Buffering": "no",
}
# (request, identity, seconds) -> the Investigation Service's stream of WatchInvestigation items
Watch = Callable[[pb.WatchInvestigationRequest, str, float], AsyncGenerator[Any]]


class StartInvestigation(BaseModel):
    turbine_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=500)
    # How urgent the person judges it: "low", "medium", "high" or "critical".
    severity: str = Field(pattern=r"^(low|medium|high|critical)$")


class Investigation(BaseModel):
    case_id: str
    turbine_id: str
    title: str
    # "open", "investigating", "pending_review", "approved", "verifying", "verified", "closed" or
    # "cancelled".
    status: str
    severity: str
    # "waiting" for its evidence, "running", "completed", "ended" or "not_started"; "" for a case
    # with no investigation.
    investigation_status: str
    run_id: str
    created_at: str | None
    updated_at: str | None
    # Where to follow it.
    events: str


class Detection(BaseModel):
    detection_id: str
    rule_id: str
    rule_version: str
    severity: str
    attached_at: str | None


class CaseEvent(BaseModel):
    at: str | None
    action: str
    actor_type: str
    actor_id: str
    note: str


class MetricComparison(BaseModel):
    metric: str
    unit: str
    before_mean: float | None
    after_mean: float | None
    before_maximum: float | None
    after_maximum: float | None
    before_readings: int
    after_readings: int


class RuleResult(BaseModel):
    rule_id: str
    version: int
    severity: str
    summary: str
    counts: bool


class Verification(BaseModel):
    work_order_id: str
    proposal_version: int
    completed_by: str
    completed_at: str | None
    # "waiting", "reported" or "not_started".
    status: str
    # "resolved", "not_resolved" or "inconclusive", once reported.
    outcome: str
    window_start: str | None
    window_end: str | None
    after_samples: int
    metrics: list[MetricComparison]
    rules: list[RuleResult]
    note: str
    reported_at: str | None
    # "closed" or "reopened", once a person answered.
    review: str
    reviewed_by: str
    reviewed_at: str | None
    review_reason: str


class CaseDetail(BaseModel):
    investigation: Investigation
    # "detection" or "user".
    origin: str
    created_by: str
    version: int
    evidence_snapshot_id: str
    evidence_version: int
    # "pending" or "ready".
    evidence_status: str
    stale_rule: str
    stale_detail: str
    detections: list[Detection]
    # Oldest first.
    history: list[CaseEvent]
    # Newest first.
    verifications: list[Verification]


class FindingReview(BaseModel):
    review_id: str
    run_id: str
    # Where in the report, such as "hypotheses[0]".
    finding: str
    # "accept", "reject", "challenge" or "annotate".
    action: str
    note: str
    reviewed_by: str
    reviewed_at: str | None


class Report(BaseModel):
    run_id: str
    # The investigation's report (SRS 5.7), or null before it completes.
    report: dict[str, Any] | None
    # Oldest first.
    reviews: list[FindingReview]


class ReviewIn(BaseModel):
    finding: str = Field(pattern=r"^[a-z_]{1,40}\[\d{1,4}\]$")
    action: str = Field(pattern=r"^(accept|reject|challenge|annotate)$")
    # Why. Required, except to accept.
    note: str = Field(default="", max_length=1000)


class VerificationAnswer(BaseModel):
    # "close" or "reopen".
    decision: str = Field(pattern=r"^(close|reopen)$")
    # Why. Required to reopen.
    reason: str = Field(default="", max_length=500)
    # The case version the person saw.
    expected_version: int = Field(ge=1)


def _optional(message: Any, name: str) -> float | None:
    return float(getattr(message, name)) if message.HasField(name) else None


def _verification(v: Any) -> Verification:
    return Verification(
        work_order_id=v.work_order_id, proposal_version=v.proposal_version,
        completed_by=v.completed_by, completed_at=time_of(v, "completed_at"), status=v.status,
        outcome=v.outcome, window_start=time_of(v, "window_start"),
        window_end=time_of(v, "window_end"), after_samples=v.after_samples,
        metrics=[MetricComparison(
            metric=m.metric, unit=m.unit, before_mean=_optional(m, "before_mean"),
            after_mean=_optional(m, "after_mean"), before_maximum=_optional(m, "before_maximum"),
            after_maximum=_optional(m, "after_maximum"), before_readings=m.before_readings,
            after_readings=m.after_readings) for m in v.metrics],
        rules=[RuleResult(rule_id=r.rule_id, version=r.version, severity=r.severity,
                          summary=r.summary, counts=r.counts) for r in v.rules],
        note=v.note, reported_at=time_of(v, "reported_at"), review=v.review,
        reviewed_by=v.reviewed_by, reviewed_at=time_of(v, "reviewed_at"),
        review_reason=v.review_reason,
    )  # fmt: skip


def _detail(answer: Any) -> CaseDetail:
    case = answer.case
    return CaseDetail(
        investigation=_investigation(case), origin=case.origin, created_by=case.created_by,
        version=case.version, evidence_snapshot_id=case.evidence_snapshot_id,
        evidence_version=case.evidence_version, evidence_status=case.evidence_status,
        stale_rule=case.stale_rule, stale_detail=case.stale_detail,
        detections=[Detection(detection_id=d.detection_id, rule_id=d.rule_id,
                              rule_version=d.rule_version, severity=d.severity,
                              attached_at=time_of(d, "attached_at")) for d in answer.detections],
        history=[CaseEvent(at=time_of(h, "at"), action=h.action, actor_type=h.actor_type,
                           actor_id=h.actor_id, note=h.note) for h in answer.history],
        verifications=[_verification(v) for v in answer.verifications],
    )  # fmt: skip


def _review(r: Any) -> FindingReview:
    return FindingReview(review_id=r.review_id, run_id=r.run_id, finding=r.finding,
                         action=r.action, note=r.note, reviewed_by=r.reviewed_by,
                         reviewed_at=time_of(r, "reviewed_at"))  # fmt: skip


def _investigation(case: Any) -> Investigation:
    return Investigation(
        case_id=case.case_id, turbine_id=case.turbine_id, title=case.title, status=case.status,
        severity=case.severity, investigation_status=case.investigation_status,
        run_id=case.run_id, created_at=time_of(case, "created_at"),
        updated_at=time_of(case, "updated_at"),
        events=f"/api/v1/investigations/{case.case_id}/events",
    )  # fmt: skip


def event_of(event: Any) -> str:
    """One server-sent event: its sequence as the ID, its kind as the event's name."""
    data = {
        "sequence": event.sequence, "at": time_of(event, "at"), "kind": event.kind,
        "run_id": event.run_id, "position": event.position, "total_steps": event.total_steps,
        "step": event.step, "status": event.status, "note": event.note,
    }  # fmt: skip
    return f"id: {event.sequence}\nevent: {event.kind}\ndata: {json.dumps(data)}\n\n"


def add_investigation_routes(
    router: APIRouter,
    caller: Callable[..., Awaitable[Caller]],
    call_as: Call,
    watch: Watch | None,
    identity_for: Callable[[Caller], str],
    failed: Callable[[grpc.aio.AioRpcError], Exception],
) -> None:
    @router.post(
        "/investigations", response_model=Investigation, status_code=202,
        summary="Open a case and investigate it: the case's ID at once, the run once its evidence "
        "is frozen",
    )  # fmt: skip
    async def start(
        body: StartInvestigation, key: Key, request: Request,
        who: Annotated[Caller, Depends(caller)],
    ) -> Investigation:  # fmt: skip
        create = pb.CreateCaseRequest(
            turbine_id=body.turbine_id, title=body.title, severity=body.severity,
            idempotency_key=key, investigate=True,
            # The person's own token, verified above, proves to the Identity Service that they
            # are signed in now, for the delegation their investigation's agents act under.
            subject_token=bearer(request.headers.get("authorization")),
        )  # fmt: skip
        answer = await call_as("investigation", "CreateCase", create, who, retry=False)
        return _investigation(answer.case)

    @router.get(
        "/investigations", response_model=list[Investigation],
        summary="Cases the person may read: the active ones unless every status is asked for",
    )  # fmt: skip
    async def list_cases(
        who: Annotated[Caller, Depends(caller)],
        every: Annotated[bool, Query(alias="all", description="Closed and cancelled too")] = False,
        turbine_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]{0,64}$")] = "",
    ) -> list[Investigation]:  # fmt: skip
        statuses = list(STATUSES) if every else []
        request = pb.ListCasesRequest(statuses=statuses, turbine_id=turbine_id, page_size=200)
        answer = await call_as("investigation", "ListCases", request, who)
        return [_investigation(case) for case in answer.cases]

    @router.get(
        "/investigations/{case_id}/detail", response_model=CaseDetail,
        summary="A case with its detections, history and verifications",
    )  # fmt: skip
    async def detail(case_id: CaseId, who: Annotated[Caller, Depends(caller)]) -> CaseDetail:
        answer = await call_as("investigation", "GetCase", pb.GetCaseRequest(case_id=case_id), who)
        return _detail(answer)

    @router.get(
        "/investigations/{case_id}/report", response_model=Report,
        summary="The investigation's report, and every review of its findings (FR-INV-006)",
    )  # fmt: skip
    async def report(case_id: CaseId, who: Annotated[Caller, Depends(caller)]) -> Report:
        request = pb.GetInvestigationReportRequest(case_id=case_id)
        answer = await call_as("investigation", "GetInvestigationReport", request, who)
        return Report(
            run_id=answer.run_id,
            report=json.loads(answer.report_json) if answer.report_json else None,
            reviews=[_review(r) for r in answer.reviews],
        )

    @router.post(
        "/investigations/{case_id}/findings", response_model=FindingReview, status_code=201,
        summary="Accept, reject, challenge or annotate one finding. It decides nothing",
    )  # fmt: skip
    async def review_finding(
        case_id: CaseId, body: ReviewIn, key: Key, who: Annotated[Caller, Depends(caller)],
    ) -> FindingReview:  # fmt: skip
        request = pb.ReviewFindingRequest(case_id=case_id, finding=body.finding,
                                          action=body.action, note=body.note,
                                          idempotency_key=key)  # fmt: skip
        answer = await call_as("investigation", "ReviewFinding", request, who, retry=False)
        return _review(answer.review)

    @router.post(
        "/investigations/{case_id}/verification", response_model=CaseDetail,
        summary="Close a verified case, or reopen it to be investigated again",
    )  # fmt: skip
    async def answer_verification(
        case_id: CaseId, body: VerificationAnswer, key: Key,
        who: Annotated[Caller, Depends(caller)],
    ) -> CaseDetail:  # fmt: skip
        request = pb.ReviewVerificationRequest(
            case_id=case_id, decision=body.decision, reason=body.reason,
            expected_version=body.expected_version, idempotency_key=key,
        )  # fmt: skip
        await call_as("investigation", "ReviewVerification", request, who, retry=False)
        answer = await call_as("investigation", "GetCase", pb.GetCaseRequest(case_id=case_id), who)
        return _detail(answer)

    @router.get(
        "/investigations/{case_id}", response_model=Investigation,
        summary="A case and where its investigation is",
    )  # fmt: skip
    async def get(case_id: CaseId, who: Annotated[Caller, Depends(caller)]) -> Investigation:
        answer = await call_as("investigation", "GetCase", pb.GetCaseRequest(case_id=case_id), who)
        return _investigation(answer.case)

    @router.get(
        "/investigations/{case_id}/events",
        summary="The investigation's progress as server-sent events, resumable with Last-Event-ID",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def events(
        case_id: CaseId, who: Annotated[Caller, Depends(caller)],
        last_event_id: Annotated[
            str, Header(alias="Last-Event-ID", pattern=r"^\d{1,18}$",
                        description="The last event's ID already seen")
        ] = "0",
    ) -> StreamingResponse:  # fmt: skip
        # Never past the person's own token, nor longer than a stream lasts.
        seconds = float(min(STREAM_SECONDS, max(who.expires_at - int(time.time()), 0)))
        request = pb.WatchInvestigationRequest(case_id=case_id, after_sequence=int(last_event_id))
        if watch is None:
            raise failed(grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(),
                                              grpc.aio.Metadata()))  # fmt: skip
        stream = watch(request, identity_for(who), seconds)
        try:
            # The first answer comes before the stream does, so a case the person may not read
            # is a 404 like any other, not an empty stream.
            first = await anext(stream, None)
        except grpc.aio.AioRpcError as error:
            await stream.aclose()
            raise failed(error) from error

        async def send() -> AsyncIterator[str]:
            yield f"retry: {RETRY_MILLISECONDS}\n\n"
            item = first
            try:
                while item is not None:
                    kind = item.WhichOneof("item")
                    if kind == "event":
                        yield event_of(item.event)
                    elif kind == "heartbeat":
                        yield ": still investigating\n\n"
                    elif kind == "finished":
                        yield "event: finished\ndata: {}\n\n"
                        return
                    item = await anext(stream, None)
            except (grpc.aio.AioRpcError, TimeoutError):
                # The stream ends, and the client carries on from its Last-Event-ID.
                return
            finally:
                await stream.aclose()

        return StreamingResponse(send(), media_type="text/event-stream", headers=SSE_HEADERS)
