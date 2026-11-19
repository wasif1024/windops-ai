"""A stand-in for Anthropic's Messages API, for dry runs at no cost (M8.13, ADR 0027).

A rehearsal replaces each agent's model with a script, so it never exercises the path to Claude:
the Claude client, its HTTP requests and their tools, adaptive thinking replayed through the
history guard, structured outputs for step 10, the budget, and the LLM metrics. A dry run keeps
all of that, and answers it here instead of at Anthropic.

It listens on 127.0.0.1 only, inside the runtime's own pod, and only when the runtime is started
with `WINDOPS_AGENT_STAND_IN=on`. A run asks for it with `stand_in=true`; the runtime then gives
that run's Claude client this address and a placeholder key, never the real one. Its calls are
labelled `provider="stand-in"`, which the LLM Operations dashboard leaves out.

It plays the team as the rehearsal does: the Supervisor reads the evidence and hands work to
every specialist; each specialist makes its rehearsal's one free tool call and answers; step 10
gets a result that cites what the tools returned. It never decides anything a model would.
"""

import asyncio
import contextlib
import hashlib
import itertools
import json
import re
import socket
from collections.abc import Mapping
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from windops_agent_runtime.team import rehearsal
from windops_agent_runtime.team.definitions import SUPERVISOR, team

PLACEHOLDER_KEY = "stand-in-placeholder"
_CASE = re.compile(r"case_[a-z0-9]{20}")
_SNAPSHOT = re.compile(r"evs_[a-z0-9]{20}")
_TURBINE_FIELD = re.compile(r'"turbine_id":\s*"([A-Za-z0-9_.:-]{1,64})"')
_TURBINE_TEXT = re.compile(r"turbine ([A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}[A-Za-z0-9_-])")
_ids = itertools.count(1)


def _text(content: Any) -> str:
    """Every piece of text in a message's content, tool results included."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            parts.append(_text(block.get("content")))
    return "\n".join(parts)


def _tool_use(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_standin{next(_ids):012d}", "name": name,
            "input": dict(arguments)}  # fmt: skip


def _message(body: Mapping[str, Any], blocks: list[dict[str, Any]], stop: str) -> dict[str, Any]:
    """A Messages API answer, with a signed thinking block, as adaptive thinking gives."""
    thought = f"Stand-in reasoning for {stop}"
    signature = hashlib.sha256(json.dumps(blocks, sort_keys=True).encode()).hexdigest()
    content = [{"type": "thinking", "thinking": thought, "signature": signature}, *blocks]
    asked = len(json.dumps(body.get("messages", []))) + len(json.dumps(body.get("tools", [])))
    return {
        "id": f"msg_standin{next(_ids):012d}", "type": "message", "role": "assistant",
        "model": body["model"], "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": max(1, asked // 4), "output_tokens": max(1, len(
            json.dumps(content)) // 4), "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": len(str(body.get("system", ""))) // 4},
    }  # fmt: skip


def _structured(body: Mapping[str, Any]) -> bool:
    """Whether the request asks for structured outputs, as step 10 does."""
    return "json_schema" in json.dumps({k: v for k, v in body.items() if k != "messages"})


def _specialist_of(tools: set[str]) -> str:
    """Which specialist is asking, from the tools it was given."""
    best, overlap = "", 0
    for name, agent in team().agents.items():
        if name == SUPERVISOR:
            continue
        own = {".".join(s.split(".")[1:]).replace(".", "_") if s.startswith("mcp.")
               else s.replace(".", "_") for s in agent.tools}  # fmt: skip
        if own <= tools and len(own) > overlap:
            best, overlap = name, len(own)
    return best


def answer(body: Mapping[str, Any]) -> dict[str, Any]:
    """The stand-in's answer to one Messages API request."""
    messages = list(body.get("messages", []))
    tools = {str(t.get("name")) for t in body.get("tools", [])}
    turns = sum(1 for m in messages if m.get("role") == "assistant")
    everything = "\n".join(_text(m.get("content")) for m in messages)
    case = (_CASE.findall(everything) or [""])[0]
    snapshot = (_SNAPSHOT.findall(everything) or [""])[0]
    turbine = next(iter(_TURBINE_FIELD.findall(everything) or _TURBINE_TEXT.findall(everything)),
                   "")  # fmt: skip
    if _structured(body):
        return _message(body, [{"type": "text", "text": json.dumps(_draft(everything, case,
                                                                          snapshot, turbine))}],
                        "end_turn")  # fmt: skip
    if "task" in tools:
        return _supervisor(body, turns, case, snapshot, turbine)
    return _specialist(body, tools, turns, case, snapshot, turbine)


def _supervisor(body: Mapping[str, Any], turns: int, case: str, snapshot: str,
                turbine: str) -> dict[str, Any]:  # fmt: skip
    if turns == 0:
        call = _tool_use("windops_get_evidence", {"snapshot_id": snapshot, "limit": 5})
        return _message(body, [call], "tool_use")
    if turns == 1:
        # Every specialist at once, as Claude may: Deep Agents runs them in parallel.
        calls = [
            _tool_use("task", {"subagent_type": name, "description": (
                f"Investigate case {case}, snapshot {snapshot}, on turbine {turbine}: report "
                "what your tools show.")})
            for name in team().agents[SUPERVISOR].delegates
        ]  # fmt: skip
        return _message(body, calls, "tool_use")
    report = (f"Stand-in report for case {case} on turbine {turbine}: each specialist read its "
              f"tools, and the evidence in snapshot {snapshot} was reviewed.")  # fmt: skip
    return _message(body, [{"type": "text", "text": report}], "end_turn")


def _specialist(body: Mapping[str, Any], tools: set[str], turns: int, case: str,
                snapshot: str, turbine: str) -> dict[str, Any]:  # fmt: skip
    name = _specialist_of(tools)
    script = rehearsal.scripts(case, snapshot, turbine).get(name, [])
    first = script[0].tool_calls[0] if script and script[0].tool_calls else None
    if turns == 0 and first is not None and first["name"] in tools:
        return _message(body, [_tool_use(first["name"], first["args"])], "tool_use")
    return _message(body, [{"type": "text", "text": f"{name or 'specialist'}: stand-in answer, "
                            "from the tool's result above."}], "end_turn")  # fmt: skip


def _draft(brief: str, case: str, snapshot: str, turbine: str) -> dict[str, Any]:
    """Step 10's result: built from the brief's own list of references, so it cites only what
    the tools returned, as the checks require."""
    listed = brief.split("References you may cite, exactly as written:", 1)[-1]
    listed = listed.split("\n\n", 1)[0]
    references = [line.strip() for line in listed.splitlines() if line.strip()]
    cited = [r for r in references if r not in (case, snapshot, turbine)][:2] or [snapshot]
    return {
        "case_id": case, "asset_id": turbine, "snapshot_id": snapshot,
        "observed_facts": [{"statement": "The stand-in read the case's evidence",
                            "citations": cited[:1]}],
        "hypotheses": [{"statement": "A stand-in hypothesis, for the checks only",
                        "support": cited[:1], "against": [], "confidence": 0.1}],
        "evidence_references": cited, "conflicting_evidence": [],
        "data_gaps": ["A dry run draws no conclusions: no model reasoned over the evidence."],
        "risk_flags": [],
        "recommended_actions": [{"action": "inspection",
                                 "summary": "Dry run: inspect the component the case concerns",
                                 "requires_climb": True, "downtime_hours": None,
                                 "impact_result_id": ""}],
        "impact": None, "confidence": 0.1,
        "explanation": "A dry run of the team against a stand-in API.",
        "escalation_required": True,
    }  # fmt: skip


async def _messages(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        return JSONResponse(answer(body))
    except (KeyError, TypeError, ValueError) as error:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": f"Stand-in: {type(error).__name__}"}},
            status_code=400,
        )  # fmt: skip


class StandIn:
    """The stand-in API, served on a free port on 127.0.0.1 for the runtime's whole life."""

    def __init__(self) -> None:
        self.url: str | None = None

    async def serve(self, stop: asyncio.Event) -> None:
        app = Starlette(routes=[Route("/v1/messages", _messages, methods=["POST"])])
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        # log_config=None: uvicorn would otherwise replace the runtime's own JSON logging.
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        self.url = f"http://127.0.0.1:{port}"
        try:
            await stop.wait()
        finally:
            self.url = None
            server.should_exit = True
            with contextlib.suppress(asyncio.CancelledError):
                await task
