"""Agents' MCP tools: each call with a fresh agent token, over mutual TLS (ADR 0019, 0020, 0021).

An agent's tools are exactly those its definition names, which match its allowlist. Each one,
when called, asks the Identity Service for a 120-second token for that agent and that one tool,
and calls the MCP server with it, presenting the runtime's own certificate. The token goes only
into the request's Authorization header: never into a prompt, a tool result or state (SEC-008).

A tool's input schema is the server's own, read from its tool list once per process. Its result
is the server's structured answer, as JSON. Text the server marked untrusted stays wrapped as it
came, so the agent sees it as data (MCP-005).
"""

import json
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from langchain_core.tools import BaseTool, StructuredTool
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from windops_agent_runtime.team import counted
from windops_agent_runtime.team.sandbox import SandboxRun, SandboxTasks, is_sandbox

NAMESPACE = "windops-mcp"
TIMEOUT_SECONDS = 60.0


class Tokens(Protocol):
    async def for_tool(self, agent: str, audience: str, scopes: list[str]) -> str: ...


@dataclass(frozen=True)
class Scope:
    server: str
    tool: str

    @classmethod
    def parse(cls, scope: str) -> "Scope":
        prefix, server, tool = scope.split(".", 2)
        if prefix != "mcp":
            raise ValueError(f"Not a tool scope: {scope}")
        return cls(server, tool)

    @property
    def audience(self) -> str:
        return f"windops-mcp-{self.server}"

    @property
    def scope(self) -> str:
        return f"mcp.{self.server}.{self.tool}"

    @property
    def name(self) -> str:
        """The name the agent calls it by, unique across servers."""
        return f"{self.server}_{self.tool}"


class McpClient:
    """Calls a tool on one of the cluster's MCP servers, with a given token."""

    def __init__(self, tls_dir: Path, url: Callable[[str], str] | None = None) -> None:
        self._context = ssl.create_default_context(cafile=str(tls_dir / "ca.crt"))
        self._context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._context.load_cert_chain(str(tls_dir / "tls.crt"), str(tls_dir / "tls.key"))
        self._url = url or (lambda audience: f"https://{audience}.{NAMESPACE}.svc:8443/mcp")

    def _http(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._context, timeout=TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}"},
        )  # fmt: skip

    async def describe(self, audience: str, token: str) -> dict[str, dict[str, Any]]:
        """The tools this token may call, with their descriptions and input schemas."""
        async with (
            self._http(token) as http,
            streamable_http_client(self._url(audience), http_client=http,
                                   terminate_on_close=False) as (reader, writer, _),
            ClientSession(reader, writer) as session,
        ):  # fmt: skip
            await session.initialize()
            listed = await session.list_tools()
        return {
            tool.name: {"description": tool.description or "", "schema": tool.inputSchema}
            for tool in listed.tools
        }

    async def call(self, audience: str, token: str, tool: str, arguments: dict[str, Any]) -> str:
        async with (
            self._http(token) as http,
            streamable_http_client(self._url(audience), http_client=http,
                                   terminate_on_close=False) as (reader, writer, _),
            ClientSession(reader, writer) as session,
        ):  # fmt: skip
            await session.initialize()
            result = await session.call_tool(tool, arguments)
        if result.structuredContent is not None and not result.isError:
            return json.dumps(result.structuredContent)
        text = "".join(getattr(part, "text", "") for part in result.content)
        return text if not result.isError else json.dumps({"error": text})


def outcome_of(answer: str) -> str:
    """ "ok", or "error" for an answer that is the server's error, such as a refusal."""
    try:
        parsed = json.loads(answer)
    except ValueError:
        return "ok"
    return "error" if isinstance(parsed, dict) and set(parsed) == {"error"} else "ok"


class ToolBox:
    """Builds an agent's tools, with each server's schemas read once per process. Sandbox tasks
    come through the Agent Sandbox Adapter, when the runtime has it (ADR 0024)."""

    def __init__(self, client: McpClient, sandbox: SandboxTasks | None = None) -> None:
        self._client = client
        self._sandbox = sandbox
        self._described: dict[str, dict[str, Any]] = {}

    async def _description(self, scope: Scope, agent: str, tokens: Tokens) -> dict[str, Any]:
        if scope.scope not in self._described:
            token = await tokens.for_tool(agent, scope.audience, [scope.scope])
            listed = await self._client.describe(scope.audience, token)
            if scope.tool not in listed:
                raise ValueError(f"{scope.audience} does not offer {scope.tool}")
            self._described[scope.scope] = listed[scope.tool]
        return self._described[scope.scope]

    async def call(self, agent: str, scope: str, tokens: Tokens, arguments: dict[str, Any]) -> str:
        """One call the runtime makes itself, as an agent, with that agent's own token."""
        parsed = Scope.parse(scope)
        token = await tokens.for_tool(agent, parsed.audience, [parsed.scope])
        return await self._client.call(parsed.audience, token, parsed.tool, arguments)

    async def tools_for(
        self,
        agent: str,
        scopes: tuple[str, ...],
        tokens: Tokens,
        before_call: Callable[[str], None] | None = None,
        after_call: Callable[[str, str], None] | None = None,
        sandbox_run: SandboxRun | None = None,
    ) -> list[BaseTool]:
        """The agent's tools. before_call runs first on each call, as to count it; after_call
        gets each answer, as to note what it may be cited by. Sandbox tasks need the run they
        are part of, where their use is recorded."""
        built: list[BaseTool] = []
        for raw in scopes:
            if is_sandbox(raw):
                if self._sandbox is None or sandbox_run is None:
                    raise ValueError(f"{agent} needs {raw}, but the runtime has no Sandbox")
                built.append(
                    await self._sandbox.tool(agent, raw, sandbox_run, before_call, after_call)
                )
                continue
            scope = Scope.parse(raw)
            described = await self._description(scope, agent, tokens)
            built.append(self._tool(agent, scope, described, tokens, before_call, after_call))
        return built

    def _tool(
        self,
        agent: str,
        scope: Scope,
        described: dict[str, Any],
        tokens: Tokens,
        before_call: Callable[[str], None] | None,
        after_call: Callable[[str, str], None] | None = None,
    ) -> BaseTool:
        client = self._client

        async def call(**arguments: Any) -> str:
            if before_call is not None:
                before_call(scope.name)
            labels = {"agent": agent, "server": scope.audience, "tool": scope.tool}
            try:
                # A fresh token for this one call: it never outlives the call by much (SEC-008).
                token = await tokens.for_tool(agent, scope.audience, [scope.scope])
                answer = await client.call(scope.audience, token, scope.tool, arguments)
            except Exception:
                counted.tool_calls.add(1, {**labels, "outcome": "failed"})
                raise
            counted.tool_calls.add(1, {**labels, "outcome": outcome_of(answer)})
            if after_call is not None:
                after_call(scope.name, answer)
            return answer

        return StructuredTool(
            name=scope.name,
            description=described["description"],
            args_schema=described["schema"],
            coroutine=call,
        )


def stand_in(answers: Mapping[str, Callable[[dict[str, Any]], Awaitable[str]]]) -> Any:
    """For tests: a client whose tools answer from the given functions."""

    class StandIn:
        async def describe(self, audience: str, token: str) -> dict[str, dict[str, Any]]:
            server = audience.removeprefix("windops-mcp-")
            return {
                name.removeprefix(f"{server}_"): {
                    "description": f"{name} (test)",
                    "schema": {"type": "object", "properties": {}, "additionalProperties": True},
                }
                for name in answers
                if name.startswith(f"{server}_")
            }

        async def call(
            self, audience: str, token: str, tool: str, arguments: dict[str, Any]
        ) -> str:
            server = audience.removeprefix("windops-mcp-")
            return await answers[f"{server}_{tool}"](arguments)

    return StandIn()
