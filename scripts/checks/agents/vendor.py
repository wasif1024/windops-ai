#!/usr/bin/env python3
"""Confirm the agent stack's vendor checks against the real services (M8.1, SRS 13.4, SBX-004).

Every request to Claude is recorded as it is sent and each response as it arrives, so what the
libraries do to a request is seen, not assumed. Only synthetic text is sent, and no key is printed.

Claude through LangChain and Deep Agents:
- effort is set explicitly and reaches the API (LLM-001, LLM-003), on Opus 5.5 and Haiku 5.5;
- server-side refusal fallback is accepted, and a fallback block survives replay (LLM-006);
- forced tool choice is refused, structured outputs and strict tools work without it (LLM-004);
- stop reasons reach the caller, `max_tokens` among them (LLM-005);
- a stable system prompt is cached and read back from the cache (LLM-007);
- history is append-only: each request replays the previous response's blocks unchanged,
  thinking blocks and signatures included, through LangChain and through a Deep Agents run
  resumed from its checkpoint (LLM-008);
- where inference ran, as the API reports it (Claude data handling).

LangSmith:
- a synthetic trace reaches the project and is read back;
- a Sandbox can be created on this plan (SBX-004), with every outbound destination denied, holds
  no credentials, and runs the first approved task, a vibration spectrum (SBX-001, SBX-002).

It costs a few cents of Claude tokens and a minute of Sandbox compute. Needs ANTHROPIC_API_KEY,
LANGSMITH_API_KEY and LANGSMITH_PROJECT in .env.
"""

import base64
import itertools
import json
import math
import os
import re
import secrets
import sys
import time
import uuid
import warnings
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from scripts.lib import settings

OPUS, HAIKU = "claude-opus-5-5", "claude-haiku-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
RUN = secrets.token_hex(3)
VERSIONS = (
    "anthropic", "langchain", "langchain-anthropic", "langchain-core", "langgraph", "deepagents",
    "langsmith",
)  # fmt: skip


class Results:
    def __init__(self) -> None:
        self.failed: list[str] = []
        self.notes: list[str] = []
        self.gaps: list[str] = []

    def check(self, condition: bool, message: str) -> bool:
        print(f"{'ok' if condition else 'not ok'} - {message}", flush=True)
        if not condition:
            self.failed.append(message)
        return condition

    def gap(self, condition: bool, message: str) -> None:
        """A vendor condition the platform cannot meet on its own, for a person to decide."""
        if condition:
            return
        print(f"gap - {message}", flush=True)
        self.gaps.append(message)

    def note(self, message: str) -> None:
        print(f"note - {message}", flush=True)
        self.notes.append(message)

    def step(self, name: str, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception as error:  # A step's failure is reported, and the others still run.
            self.check(False, f"{name}: {type(error).__name__}: {str(error)[:300]}")


class Recorder:
    """Every request body sent to Claude, and every response body, in order."""

    def __init__(self) -> None:
        self.exchanges: list[tuple[dict[str, Any], dict[str, Any], dict[str, str]]] = []
        self._pending: dict[int, tuple[dict[str, Any], dict[str, str]]] = {}

    def on_request(self, request: Any) -> None:
        body = json.loads(request.content) if request.content else {}
        headers = {"anthropic-beta": request.headers.get("anthropic-beta", "")}
        self._pending[id(request)] = (body, headers)

    def on_response(self, response: Any) -> None:
        response.read()
        sent, headers = self._pending.pop(id(response.request), ({}, {}))
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        answer["_status"] = response.status_code
        self.exchanges.append((sent, answer, headers))

    def since(self, start: int) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, str]]]:
        return self.exchanges[start:]


def install(recorder: Recorder) -> None:
    """Make LangChain's Anthropic client record what it sends and receives."""
    import langchain_anthropic.chat_models as chat_models
    from langchain_anthropic._client_utils import _SyncHttpxClientWrapper

    def recording(**kwargs: Any) -> Any:
        return _SyncHttpxClientWrapper(
            **kwargs,
            event_hooks={"request": [recorder.on_request], "response": [recorder.on_response]},
        )

    chat_models._get_default_httpx_client = recording  # type: ignore[attr-defined, assignment]


def blocks(content: Any) -> list[dict[str, Any]]:
    """A message's content as the API judges an edit: blocks without nulls or cache markers."""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return [
        {k: v for k, v in block.items() if v is not None and k not in ("cache_control", "index")}
        for block in content
    ]


def replayed(previous: dict[str, Any], response: list[Any], following: dict[str, Any]) -> str:
    """Why a request does not carry the last request's history and response unchanged, or ""."""
    history = [*previous.get("messages", []), {"role": "assistant", "content": response}]
    sent = following.get("messages", [])
    if len(sent) < len(history):
        return f"{len(history)} messages expected at least, {len(sent)} sent"
    for index, (expected, actual) in enumerate(zip(history, sent, strict=False)):
        if expected["role"] != actual["role"] or blocks(expected["content"]) != blocks(
            actual["content"]
        ):
            kinds = [b.get("type") for b in blocks(expected["content"])]
            changed = [b.get("type") for b in blocks(actual["content"])]
            return f"message {index} changed: {kinds} became {changed}"
    return ""


def append_only(
    exchanges: list[tuple[dict[str, Any], dict[str, Any], dict[str, str]]],
) -> list[str]:
    """Each conversation's requests, by its system prompt, checked one against the next."""
    problems = []
    by_conversation: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for sent, raw, _ in exchanges:
        key = json.dumps(blocks(sent.get("system") or []), sort_keys=True)
        by_conversation.setdefault(key, []).append((sent, raw))
    for conversation in by_conversation.values():
        for (before, raw), (after, _) in itertools.pairwise(conversation):
            why = replayed(before, raw.get("content", []), after)
            if why:
                problems.append(why)
    return problems


def check_effort(results: Results, recorder: Recorder) -> None:
    from langchain_anthropic import ChatAnthropic

    for model, effort in ((OPUS, "high"), (HAIKU, "low")):
        start = len(recorder.exchanges)
        llm = ChatAnthropic(model=model, effort=effort, max_tokens=2048)  # type: ignore[call-arg, arg-type]
        answer = llm.invoke("Reply with the single word: ready")
        sent, raw, _ = recorder.since(start)[-1]
        results.check(
            sent.get("output_config", {}).get("effort") == effort
            and sent.get("model") == model
            and (sent.get("thinking") or {"type": "adaptive"}).get("type") == "adaptive"
            and answer.response_metadata.get("stop_reason") == "end_turn",
            f"{model} takes effort {effort} as set, explicitly, in output_config",
        )  # fmt: skip
        geo = (raw.get("usage") or {}).get("inference_geo") or "not reported"
        results.note(f"{model} ran with inference_geo {geo}, thinking {sent.get('thinking')}")


def check_fallback(results: Results, recorder: Recorder) -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import AIMessage, HumanMessage

    start = len(recorder.exchanges)
    llm = ChatAnthropic(  # type: ignore[call-arg]
        model=OPUS, effort="low", max_tokens=2048, betas=[FALLBACK_BETA],
        model_kwargs={"fallbacks": "default"},
    )  # fmt: skip
    answer = llm.invoke("Name one renewable energy source, in one word.")
    sent, raw, headers = recorder.since(start)[-1]
    results.check(
        sent.get("fallbacks") == "default" and FALLBACK_BETA in headers["anthropic-beta"]
        and raw["_status"] == 200 and answer.response_metadata.get("stop_reason") == "end_turn",
        "Opus 5.5 accepts server-side refusal fallback, sent through LangChain (LLM-006)",
    )  # fmt: skip
    # A refusal cannot be asked for on purpose. What a fallback leaves in the history must still
    # go back unchanged, or the next turn would be an edited history.
    block = {"type": "fallback", "from": {"model": OPUS}, "to": {"model": "claude-opus-5"}}
    history = [HumanMessage("q"), AIMessage(content=[block, {"type": "text", "text": "a"}]),
               HumanMessage("next")]  # fmt: skip
    payload = llm._get_request_payload(history)
    results.check(
        payload["messages"][1]["content"][0] == block,
        "A fallback block in the history is sent back as it came",
    )


class Reading(BaseModel):
    """A gearbox reading."""

    turbine_id: str = Field(description="The turbine")
    oil_temperature_c: float = Field(description="Oil temperature in °C")


def check_structured(results: Results, recorder: Recorder) -> None:
    import anthropic
    from langchain_anthropic import ChatAnthropic
    from langchain_core.tools import tool

    @tool
    def gearbox_reading(turbine_id: str) -> str:
        """Read a turbine's gearbox oil temperature."""
        return json.dumps({"turbine_id": turbine_id, "oil_temperature_c": 71.5})

    llm = ChatAnthropic(model=OPUS, effort="low", max_tokens=4096)  # type: ignore[call-arg]
    refused = False
    try:
        llm.bind_tools([gearbox_reading], tool_choice="any").invoke("Read T01.")
    except anthropic.BadRequestError:
        refused = True
    results.check(refused, "Forced tool choice is refused by Opus 5.5, as LLM-004 expects")

    start = len(recorder.exchanges)
    parsed = llm.with_structured_output(Reading, method="json_schema").invoke(
        "Turbine T01's gearbox oil is at 71.5 °C. Extract the reading."
    )
    sent, _, _ = recorder.since(start)[-1]
    results.check(
        isinstance(parsed, Reading) and parsed.oil_temperature_c == 71.5
        and "format" in sent.get("output_config", {}) and "tool_choice" not in sent,
        "Structured output comes back as a validated model, through output_config.format",
    )  # fmt: skip
    start = len(recorder.exchanges)
    llm.with_structured_output(Reading).invoke("T02's gearbox oil is at 64 °C. Extract it.")
    sent, _, _ = recorder.since(start)[-1]
    choice = (sent.get("tool_choice") or {}).get("type", "none")
    results.note(
        f"LangChain's default structured-output method sends tool_choice {choice} on Opus 5.5, "
        "never forced; the agents use method='json_schema'"
    )

    start = len(recorder.exchanges)
    strict = llm.bind_tools([gearbox_reading], strict=True).invoke(
        "Use the gearbox_reading tool for turbine T03, then stop."
    )
    sent, _, _ = recorder.since(start)[-1]
    calls = strict.tool_calls
    results.check(
        all(t.get("strict") is True for t in sent.get("tools", []))
        and bool(calls)
        and calls[0]["args"] == {"turbine_id": "T03"},
        "A strict tool is called with schema-valid arguments, with tool choice left to Claude",
    )


def check_stop_reasons(results: Results) -> None:
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model=OPUS, effort="low", max_tokens=32)  # type: ignore[call-arg]
    cut = llm.invoke("Write a 500-word essay on wind turbine gearboxes.")
    results.check(
        cut.response_metadata.get("stop_reason") == "max_tokens",
        "A cut-off answer reports stop_reason max_tokens to the caller (LLM-005)",
    )
    results.note(
        "refusal: LangChain puts stop_reason and stop_details in response_metadata, as its "
        "source shows; a refusal cannot be asked for on purpose. pause_turn comes only from "
        "server tools, which the agents do not use"
    )


POLICY = "\n".join(
    f"Rule {n}: a maintenance proposal for component group {n % 17} must cite the evidence "
    f"snapshot, the telemetry window, the rule version and the safety procedure revision it rests "
    f"on, and state its uncertainty; it may never approve, submit or reserve on its own."
    for n in range(260)
)


def check_caching(results: Results, recorder: Recorder) -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = ChatAnthropic(model=OPUS, effort="low", max_tokens=1024)  # type: ignore[call-arg]
    system = SystemMessage(
        content=[{"type": "text", "text": POLICY, "cache_control": {"type": "ephemeral"}}]
    )
    usages: list[dict[str, Any]] = []
    for question in ("How many rules are there? Answer with a number.",
                     "Which rule number is the last one? Answer with a number."):  # fmt: skip
        answer = llm.invoke([system, HumanMessage(question)])
        usage: dict[str, Any] = dict(answer.usage_metadata or {})
        usages.append(dict(usage.get("input_token_details") or {}))
    written = usages[0].get("cache_creation", 0) or usages[1].get("cache_creation", 0)
    read = usages[1].get("cache_read", 0)
    results.check(
        read > 0,
        f"A stable system prompt is cached: {written} tokens written, {read} read back (LLM-007)",
    )


def check_history(results: Results, recorder: Recorder) -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import tool

    @tool
    def gearbox_reading(turbine_id: str) -> str:
        """Read a turbine's gearbox oil temperature."""
        return json.dumps({"turbine_id": turbine_id, "oil_temperature_c": 71.5})

    llm = ChatAnthropic(model=OPUS, effort="high", max_tokens=8192).bind_tools(  # type: ignore[call-arg]
        [gearbox_reading]
    )
    start = len(recorder.exchanges)
    messages: list[Any] = [HumanMessage(
        "Turbine T01's gearbox oil was 62 °C six hours ago, with the load flat since. Read its "
        "temperature now with the tool, think through what the change suggests, then give the "
        "single most likely cause in one sentence."
    )]  # fmt: skip
    for _ in range(4):
        answer = llm.invoke(messages)
        messages.append(answer)
        if not answer.tool_calls:
            break
        for call in answer.tool_calls:
            messages.append(ToolMessage(gearbox_reading.invoke(call["args"]),
                                        tool_call_id=call["id"]))  # fmt: skip
    exchanges = recorder.since(start)
    results.check(
        len(exchanges) >= 2, f"A tool loop ran {len(exchanges)} requests through LangChain"
    )
    thinking = sum(
        1 for _, raw, _ in exchanges for block in raw.get("content", [])
        if block.get("type") == "thinking" and block.get("signature")
    )  # fmt: skip
    problems = append_only(exchanges)
    results.check(
        not problems and thinking > 0,
        f"Each request replays the last response's blocks unchanged, {thinking} signed thinking "
        f"blocks among them (LLM-008){': ' + '; '.join(problems) if problems else ''}",
    )


def check_deep_agents(results: Results, recorder: Recorder) -> None:
    from deepagents import create_deep_agent
    from langchain_anthropic import ChatAnthropic
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    @tool
    def gearbox_reading(turbine_id: str) -> str:
        """Read a turbine's gearbox oil temperature."""
        return json.dumps({"turbine_id": turbine_id, "oil_temperature_c": 71.5})

    agent: Any = create_deep_agent(
        model=ChatAnthropic(model=OPUS, effort="medium", max_tokens=8192),  # type: ignore[call-arg]
        tools=[gearbox_reading],
        system_prompt="You check gearbox readings for a wind farm. Be brief.",
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": f"vendor-check-{RUN}"}}
    start = len(recorder.exchanges)
    agent.invoke({"messages": [{"role": "user", "content":
                  "Read T01's gearbox oil temperature with gearbox_reading, in one line."}]},
                 config)  # fmt: skip
    # A second turn on the same thread: the history comes back from the checkpoint.
    agent.invoke({"messages": [{"role": "user", "content": "Is that above 80 °C? One word."}]},
                 config)  # fmt: skip
    exchanges = recorder.since(start)
    models = {sent.get("model") for sent, _, _ in exchanges}
    efforts = {sent.get("output_config", {}).get("effort") for sent, _, _ in exchanges}
    forced = [s.get("tool_choice") for s, _, _ in exchanges
              if (s.get("tool_choice") or {}).get("type") in ("any", "tool")]  # fmt: skip
    results.check(
        models == {OPUS} and efforts == {"medium"} and not forced,
        f"Every one of a Deep Agents run's {len(exchanges)} requests uses {OPUS} at the effort "
        "set, never a default model, and never forces a tool",
    )
    problems = append_only(exchanges)
    systems = {json.dumps(sent.get("system"), sort_keys=True) for sent, _, _ in exchanges}
    results.check(
        not problems,
        "Across turns and a checkpoint resume, Deep Agents replays each response unchanged"
        f"{': ' + '; '.join(problems) if problems else ''}",
    )
    cached = sum(1 for s, _, _ in exchanges if "cache_control" in json.dumps(s))
    results.note(
        f"Deep Agents sent {len(systems)} distinct system prompts across the run, and marked "
        f"{cached} of {len(exchanges)} requests for caching"
    )
    tools = sorted({t.get("name") for s, _, _ in exchanges for t in s.get("tools", [])})
    results.note(
        f"Deep Agents gave the model these tools by default: {', '.join(tools)}. Its file and "
        "task tools, and execute with a sandbox backend, must be removed or confined before "
        "agents use it (SRS 9.5)"
    )
    results.note(
        "Deep Agents adds summarization middleware by default, which rewrites earlier history "
        "when context grows: the Agent Runtime must exclude it (LLM-008)"
    )


def check_langsmith(results: Results) -> None:
    from langsmith import Client, traceable, tracing_context

    project = settings.get("LANGSMITH_PROJECT")
    run_id = uuid.uuid4()

    @traceable(name="windops-vendor-check", run_type="chain")
    def synthetic(question: str) -> str:
        return f"synthetic answer to: {question}"

    client = Client()
    with tracing_context(enabled=True, project_name=project, client=client):
        synthetic("synthetic vendor check", langsmith_extra={"run_id": run_id})
    client.flush()
    found = None
    deadline = time.monotonic() + 60
    while found is None and time.monotonic() < deadline:
        try:
            with warnings.catch_warnings():
                # read_run is supported until 2027-01-31; its successor needs the project's ID.
                warnings.simplefilter("ignore")
                found = client.read_run(run_id)
        except Exception:
            time.sleep(3)
    results.check(
        found is not None and found.name == "windops-vendor-check",
        f"A synthetic trace reaches LangSmith project {project!r} and is read back",
    )


def spectrum_task() -> str:
    """The first approved Sandbox task: the strongest frequency in a synthetic vibration."""
    return (
        "import cmath, math, json, time\n"
        "rate, n = 512, 512\n"
        "signal = [math.sin(2*math.pi*37*t/rate) + 0.4*math.sin(2*math.pi*91*t/rate) "
        "for t in range(n)]\n"
        "start = time.time()\n"
        "power = [abs(sum(signal[t]*cmath.exp(-2j*math.pi*k*t/n) for t in range(n))) "
        "for k in range(n//2)]\n"
        "peak = max(range(1, n//2), key=lambda k: power[k]) * rate / n\n"
        "print(json.dumps({'peak_hz': peak, 'seconds': round(time.time()-start, 3)}))\n"
    )


# Each prints "reached" if any answer comes back, an HTTP error included, and "blocked" if none.
NETWORK_PROBE = r"""
import json, socket, urllib.error, urllib.request
found = {}
for name, url in json.loads(TARGETS).items():
    try:
        urllib.request.urlopen(url, timeout=10)
        found[name] = "reached"
    except urllib.error.HTTPError:
        found[name] = "reached"
    except Exception:
        found[name] = "blocked"
print(json.dumps(found))
"""
# LangSmith's managed allowlist, which this check cannot close, and two places outside it.
INSIDE = {"a package registry": "https://pypi.org/simple/",
          "a model API": "https://api.anthropic.com/v1/models"}  # fmt: skip
OUTSIDE = {"an ordinary website": "https://example.com/",
           "a file-sharing site": "https://transfer.sh/"}  # fmt: skip
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)


def as_file(sandbox: Any, code: str, path: str) -> Any:
    """Write a script into the sandbox and run it, so no shell quoting can change it."""
    encoded = base64.b64encode(code.encode()).decode()
    return sandbox.run(f"echo {encoded} | base64 -d > {path} && python3 {path}", timeout=120)


def check_sandbox(results: Results, secrets_sent: list[str]) -> None:
    from langsmith.sandbox import SandboxClient

    client = SandboxClient()
    name = f"windops-vendor-check-{RUN}"
    try:
        sandbox = client.create_sandbox(
            name=name,
            persistent=False,
            idle_ttl_seconds=300,
            delete_after_stop_seconds=60,
            # Every destination this sandbox could name itself is denied (SBX-002).
            proxy_config={"access_control": {"allow_list": ["egress-denied.invalid"]}},
        )
    except Exception as error:
        results.check(False, f"A Sandbox can be created on this LangSmith plan (SBX-004): "
                             f"{type(error).__name__}: {str(error)[:200]}")  # fmt: skip
        return
    try:
        results.check(True, "A Sandbox can be created on this LangSmith plan (SBX-004)")
        targets = json.dumps({**INSIDE, **OUTSIDE})
        probe = as_file(sandbox, f"TARGETS = {targets!r}\n{NETWORK_PROBE}", "probe.py")
        found = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.exit_code == 0 else {}
        results.check(
            bool(found) and all(found.get(n) == "blocked" for n in OUTSIDE),
            f"Outside LangSmith's managed allowlist, the Sandbox reaches nothing: "
            f"{', '.join(f'{n} {found.get(n)}' for n in OUTSIDE)}",
        )
        open_inside = [n for n in INSIDE if found.get(n) == "reached"]
        results.gap(
            not open_inside,
            "SBX-002 asks for no default network. LangSmith's managed allowlist stays open "
            f"whatever the sandbox's own proxy settings say: {', '.join(open_inside)} reached. "
            "Accepted with compensating controls in ADR 0015; closing it needs an Enterprise "
            "exemption or a self-hosted Sandbox",
        )
        names = sandbox.run("env | cut -d= -f1", timeout=30).stdout.split()
        values = sandbox.run("env", timeout=30).stdout
        named = [n for n in names if SECRET_NAME.search(n)]
        carried = sum(1 for secret in secrets_sent if secret and secret in values)
        results.check(
            not named and not carried,
            f"It holds no credentials: none of its {len(names)} variables is named like one, "
            "and no key of this machine's is in it (SBX-002)",
        )
        started = time.monotonic()
        task = as_file(sandbox, spectrum_task(), "spectrum.py")
        answer = json.loads(task.stdout.strip().splitlines()[-1]) if task.exit_code == 0 else {}
        results.check(
            math.isclose(answer.get("peak_hz", 0), 37.0),
            f"It runs the vibration spectrum task: peak {answer.get('peak_hz')} Hz, computed in "
            f"{answer.get('seconds')} s, {time.monotonic() - started:.1f} s in all (SBX-001)",
        )
    finally:
        client.delete_sandbox(name)


def main() -> int:
    from importlib.metadata import version

    values = settings.load()
    for name in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        if not values.get(name):
            print(f"{name} is not set. Add it to .env", file=sys.stderr)
            return 1
        os.environ[name] = values[name]
    # The Sandbox's own connection uses Python's default certificate store, which this machine's
    # Python may lack: the public CA bundle is named for it. Verification is never turned off.
    if not os.environ.get("SSL_CERT_FILE"):
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    # Traces go to LangSmith only where this check sends them, never as a side effect.
    os.environ["LANGSMITH_TRACING"] = "false"
    print("Versions: " + ", ".join(f"{p} {version(p)}" for p in VERSIONS), flush=True)
    results, recorder = Results(), Recorder()
    install(recorder)
    results.step("effort", lambda: check_effort(results, recorder))
    results.step("fallback", lambda: check_fallback(results, recorder))
    results.step("structured output", lambda: check_structured(results, recorder))
    results.step("stop reasons", lambda: check_stop_reasons(results))
    results.step("prompt caching", lambda: check_caching(results, recorder))
    results.step("append-only history", lambda: check_history(results, recorder))
    results.step("Deep Agents", lambda: check_deep_agents(results, recorder))
    results.step("LangSmith tracing", lambda: check_langsmith(results))
    keys = [values["ANTHROPIC_API_KEY"], values["LANGSMITH_API_KEY"]]
    results.step("LangSmith Sandbox", lambda: check_sandbox(results, keys))
    if results.failed:
        print(f"Vendor check failed: {len(results.failed)} of the checks", file=sys.stderr)
        return 1
    if results.gaps:
        print(f"Vendor check passed, with {len(results.gaps)} accepted gap (ADR 0015)")
        return 0
    print("Vendor check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
