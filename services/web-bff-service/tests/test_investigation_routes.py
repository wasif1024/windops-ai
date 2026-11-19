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
