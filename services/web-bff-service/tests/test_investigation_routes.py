"""Investigations through real HTTP requests: the case's ID at once, then its progress as
resumable server-sent events (M8.11, API-003), with a fake Investigation Service."""

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import grpc
from test_admin import FakeService, stamp
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.investigation.v1 import investigation_pb2 as pb
from windops_grpc import verify_identity
from windops_web_bff.api import PREFIX

SECRET = "x" * 32
CASE = pb.Case(
    case_id="case_1", turbine_id="dogger-east-t01", title="Gearbox noise", status="open",
    severity="high", investigation_status="waiting", created_at=stamp(1_767_225_600),
)  # fmt: skip


def item(sequence: int, kind: str, **fields: Any) -> pb.WatchInvestigationResponse:
    return pb.WatchInvestigationResponse(
        event=pb.ProgressEvent(
            sequence=sequence, kind=kind, at=stamp(1_767_225_600 + sequence), **fields
        )
    )


@dataclass
class FakeWatch:
    """Streams the events after the one asked for, then what it is told to end with."""

    events: list[pb.WatchInvestigationResponse]
    ending: str = "finished"
    error: grpc.aio.AioRpcError | None = None
    asked: list[tuple[int, str, float]] = field(default_factory=list)
    closed: int = 0

    async def __call__(
        self, request: Any, identity: str, seconds: float
    ) -> AsyncGenerator[pb.WatchInvestigationResponse]:
        self.asked.append((request.after_sequence, verify_identity(identity, SECRET).user_id or "",
                           seconds))  # fmt: skip
        try:
            if self.error is not None:
                raise self.error
            for event in self.events:
                if event.event.sequence > request.after_sequence:
                    yield event
            yield pb.WatchInvestigationResponse(heartbeat=True)
            if self.ending == "finished":
                yield pb.WatchInvestigationResponse(finished=True)
        finally:
            self.closed += 1


EVENTS = [
    item(1, "requested", note="Waiting for the case's evidence to be frozen"),
    item(2, "started", run_id="run_1", total_steps=13),
    item(3, "case_status", status="investigating"),
    item(4, "step", run_id="run_1", position=1, total_steps=13, step="receive"),
]


def parse(body: str) -> list[dict[str, str]]:
    """Each server-sent event's fields; a comment line becomes {"comment": ...}."""
    parsed = []
    for block in body.split("\n\n"):
        if not block:
            continue
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            name, _, value = line.partition(": ")
            fields[name or "comment"] = value
        parsed.append(fields)
    return parsed


def test_investigating_answers_with_the_case_at_once_and_passes_on_the_persons_token() -> None:
    investigation = FakeService({"CreateCase": pb.CreateCaseResponse(case=CASE)})
    watch = FakeWatch(EVENTS)

    async def scenario(world: World) -> None:
        signed_in = token()
        answer = await world.client.post(
            f"{PREFIX}/investigations",
            json={"turbine_id": "dogger-east-t01", "title": "Gearbox noise", "severity": "high"},
            headers={**auth(signed_in), "Idempotency-Key": "k-1"},
        )
        assert answer.status_code == 202
        body = answer.json()
        assert (body["case_id"], body["investigation_status"]) == ("case_1", "waiting")
        assert body["events"] == "/api/v1/investigations/case_1/events"
        (call,) = investigation.calls
        assert call.request.investigate and call.request.subject_token == signed_in
        assert (call.request.idempotency_key, call.user_id, call.retry) == ("k-1", "u-1", False)
        missing_key = await world.client.post(
            f"{PREFIX}/investigations",
            json={"turbine_id": "dogger-east-t01", "title": "Noise", "severity": "high"},
            headers=auth(signed_in),
        )
        assert missing_key.status_code == 422

    with_bff(scenario, services={"investigation": investigation, "investigation_watch": watch})


def test_progress_streams_as_server_sent_events_and_resumes_after_the_last_seen() -> None:
    watch = FakeWatch(EVENTS)

    async def scenario(world: World) -> None:
        answer = await world.client.get(f"{PREFIX}/investigations/case_1/events",
                                        headers=auth(token()))  # fmt: skip
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith("text/event-stream")
        assert answer.headers["x-accel-buffering"] == "no"
        sent = parse(answer.text)
        assert sent[0] == {"retry": "3000"}
        events = [e for e in sent if "id" in e]
        assert [(e["id"], e["event"]) for e in events] == [
            ("1", "requested"), ("2", "started"), ("3", "case_status"), ("4", "step"),
        ]  # fmt: skip
        step = json.loads(events[-1]["data"])
        assert (step["step"], step["position"], step["total_steps"]) == ("receive", 1, 13)
        assert step["at"] == "2026-01-01T00:00:04Z"
        assert {"comment": "still investigating"} in sent
        assert sent[-1] == {"event": "finished", "data": "{}"}
        assert watch.asked[0][:2] == (0, "u-1"), "asked as the signed-in user"
        assert 0 < watch.asked[0][2] <= 300, "never past the person's token"

        resumed = await world.client.get(
            f"{PREFIX}/investigations/case_1/events",
            headers={**auth(token()), "Last-Event-ID": "2"},
        )
        assert [e["id"] for e in parse(resumed.text) if "id" in e] == ["3", "4"]
        assert watch.asked[1][0] == 2
        assert watch.closed == 2, "each stream's call is closed"

        bad = await world.client.get(
            f"{PREFIX}/investigations/case_1/events",
            headers={**auth(token()), "Last-Event-ID": "abc"},
        )
        assert bad.status_code == 422

    with_bff(scenario, services={"investigation": FakeService(), "investigation_watch": watch})


def test_a_stream_that_ends_unfinished_tells_the_client_to_carry_on() -> None:
    watch = FakeWatch(EVENTS[:2], ending="")

    async def scenario(world: World) -> None:
        answer = await world.client.get(f"{PREFIX}/investigations/case_1/events",
                                        headers=auth(token()))  # fmt: skip
        sent = parse(answer.text)
        assert sent[0] == {"retry": "3000"}
        assert all(e.get("event") != "finished" for e in sent)

    with_bff(scenario, services={"investigation": FakeService(), "investigation_watch": watch})


def test_a_case_the_person_may_not_read_is_not_found_not_an_empty_stream() -> None:
    watch = FakeWatch([], error=rpc_error(grpc.StatusCode.NOT_FOUND, "No such case"))

    async def scenario(world: World) -> None:
        answer = await world.client.get(f"{PREFIX}/investigations/case_x/events",
                                        headers=auth(token()))  # fmt: skip
        assert answer.status_code == 404
        assert answer.json()["code"] == "not_found"
        unsigned = await world.client.get(f"{PREFIX}/investigations/case_1/events")
        assert unsigned.status_code == 401
        assert len(watch.asked) == 1, "no stream without a verified token"

    with_bff(scenario, services={"investigation": FakeService(), "investigation_watch": watch})


def test_one_investigation_shows_where_it_is() -> None:
    running = pb.Case()
    running.CopyFrom(CASE)
    running.status, running.investigation_status, running.run_id = (
        "investigating", "running", "run_1",
    )  # fmt: skip
    investigation = FakeService({"GetCase": pb.GetCaseResponse(case=running)})

    async def scenario(world: World) -> None:
        body = (await world.client.get(f"{PREFIX}/investigations/case_1",
                                       headers=auth(token()))).json()  # fmt: skip
        assert (body["status"], body["investigation_status"], body["run_id"]) == (
            "investigating",
            "running",
            "run_1",
        )
        assert investigation.calls[0].request.case_id == "case_1"

    services = {"investigation": investigation, "investigation_watch": FakeWatch([])}
    with_bff(scenario, services=services)


def test_the_list_is_of_active_cases_unless_every_status_is_asked_for() -> None:
    investigation = FakeService({"ListCases": pb.ListCasesResponse(cases=[CASE])})

    async def scenario(world: World) -> None:
        listed = (await world.client.get(f"{PREFIX}/investigations", headers=auth(token()))).json()
        assert [c["case_id"] for c in listed] == ["case_1"]
        await world.client.get(f"{PREFIX}/investigations?all=true&turbine_id=dogger-east-t01",
                               headers=auth(token()))  # fmt: skip
        first, every = (call.request for call in investigation.calls)
        assert list(first.statuses) == [] and first.turbine_id == ""
        assert "closed" in every.statuses and every.turbine_id == "dogger-east-t01"

    with_bff(
        scenario, services={"investigation": investigation, "investigation_watch": FakeWatch([])}
    )


def test_a_case_shows_its_history_verification_and_report_with_its_reviews() -> None:
    verified = pb.Case()
    verified.CopyFrom(CASE)
    verified.status, verified.version = "verified", 12
    verification = pb.Verification(
        work_order_id="wo_1", proposal_version=1, status="reported", outcome="resolved",
        after_samples=120, metrics=[pb.MetricComparison(metric="METRIC_GEARBOX_TEMPERATURE",
                                                        unit="degC", before_mean=85.0)],
    )  # fmt: skip
    detail = pb.GetCaseResponse(
        case=verified, history=[pb.CaseEvent(action="created", actor_id="u-1")],
        verifications=[verification],
    )  # fmt: skip
    report = pb.GetInvestigationReportResponse(
        run_id="run_1", report_json=json.dumps({"hypotheses": [{"statement": "A valve"}]}),
        reviews=[pb.FindingReview(review_id="frv_1", finding="hypotheses[0]", action="challenge",
                                  note="Replaced last month", reviewed_by="u-engineer")],
    )  # fmt: skip
    investigation = FakeService({
        "GetCase": detail, "GetInvestigationReport": report,
        "ReviewFinding": pb.ReviewFindingResponse(review=report.reviews[0]),
        "ReviewVerification": pb.ReviewVerificationResponse(case=verified),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        headers = auth(token())
        got = (await world.client.get(f"{PREFIX}/investigations/case_1/detail",
                                      headers=headers)).json()  # fmt: skip
        assert got["version"] == 12 and got["history"][0]["action"] == "created"
        [shown] = got["verifications"]
        assert shown["outcome"] == "resolved" and shown["metrics"][0]["after_mean"] is None
        body = (await world.client.get(f"{PREFIX}/investigations/case_1/report",
                                       headers=headers)).json()  # fmt: skip
        assert body["report"]["hypotheses"][0]["statement"] == "A valve"
        assert body["reviews"][0]["action"] == "challenge"

        bad = await world.client.post(
            f"{PREFIX}/investigations/case_1/findings", headers={**headers, "Idempotency-Key": "r"},
            json={"finding": "explanation", "action": "accept"},
        )  # fmt: skip
        assert bad.status_code == 422, "only a finding in a list"
        made = await world.client.post(
            f"{PREFIX}/investigations/case_1/findings",
            headers={**headers, "Idempotency-Key": "r-1"},
            json={"finding": "hypotheses[0]", "action": "challenge", "note": "Replaced"},
        )  # fmt: skip
        assert made.status_code == 201
        sent = next(c for c in investigation.calls if c.method == "ReviewFinding")
        assert (sent.request.finding, sent.request.idempotency_key, sent.retry) == (
            "hypotheses[0]", "r-1", False)  # fmt: skip

        closed = await world.client.post(
            f"{PREFIX}/investigations/case_1/verification",
            headers={**headers, "Idempotency-Key": "v-1"},
            json={"decision": "close", "expected_version": 12},
        )  # fmt: skip
        assert closed.status_code == 200
        sent = next(c for c in investigation.calls if c.method == "ReviewVerification")
        assert (sent.request.decision, sent.request.expected_version) == ("close", 12)

    with_bff(
        scenario, services={"investigation": investigation, "investigation_watch": FakeWatch([])}
    )


def test_a_case_evidence_is_listed_read_item_by_item_and_frozen_again() -> None:
    telemetry = pb.EvidenceItem(
        source_service="telemetry", record_type="telemetry_snapshot", record_id="tsnap_1",
        version="h", data_quality_json=json.dumps({"quality": {"ok": 7190}, "gaps": 2}),
        content_json=json.dumps({"snapshot_id": "tsnap_1", "sample_count": 7200}),
        content_hash="c" * 64,
    )  # fmt: skip
    snapshot = pb.EvidenceSnapshot(
        snapshot_id="evs_1", case_id="case_1", version=2, turbine_id="dogger-east-t01",
        content_hash="h" * 64, item_count=1, items=[telemetry], verified=True,
    )  # fmt: skip
    listed = pb.EvidenceSnapshot()
    listed.CopyFrom(snapshot)
    del listed.items[:]
    investigation = FakeService({
        "ListEvidenceSnapshots": pb.ListEvidenceSnapshotsResponse(snapshots=[listed]),
        "GetEvidenceSnapshot": pb.GetEvidenceSnapshotResponse(snapshot=snapshot),
        "CreateEvidenceSnapshot": pb.CreateEvidenceSnapshotResponse(snapshot=snapshot),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        headers = auth(token())
        versions = (await world.client.get(f"{PREFIX}/investigations/case_1/evidence",
                                           headers=headers)).json()  # fmt: skip
        assert [(v["snapshot_id"], v["version"], v["items"]) for v in versions] == [
            ("evs_1", 2, [])]  # fmt: skip
        one = (await world.client.get(f"{PREFIX}/evidence/evs_1", headers=headers)).json()
        [item] = one["items"]
        assert one["verified"] and item["data_quality"]["quality"]["ok"] == 7190
        assert item["content"]["sample_count"] == 7200
        frozen = await world.client.post(f"{PREFIX}/investigations/case_1/evidence",
                                         headers={**headers, "Idempotency-Key": "f-1"})  # fmt: skip
        assert frozen.status_code == 201
        sent = next(c for c in investigation.calls if c.method == "CreateEvidenceSnapshot")
        assert (sent.request.case_id, sent.request.idempotency_key, sent.retry) == (
            "case_1", "f-1", False)  # fmt: skip

    with_bff(
        scenario, services={"investigation": investigation, "investigation_watch": FakeWatch([])}
    )
