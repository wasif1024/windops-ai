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
from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from windops.investigation.v1 import investigation_pb2 as pb

from windops_web_bff.common import ID_PATTERN, Call, Key, time_of
from windops_web_bff.tokens import Caller, bearer

CaseId = Annotated[str, Path(pattern=ID_PATTERN)]
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
    # "open", "investigating", "pending_review", "closed" or "cancelled".
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
