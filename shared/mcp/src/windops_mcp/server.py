"""An MCP server over streamable HTTP, with every control applied to every call.

Each HTTP request is checked on its own, so each tool call carries a token that was verified for
that call (MCP-001). The server keeps no sessions: a call needs nothing from an earlier one.

For each tool call, in this order:

1. the token names the tool's scope, or the call is denied;
2. the arguments match the tool's input schema, or the call is invalid;
3. the caller's rate and the tenant's quota allow it, or it is limited;
4. the handler runs within its timeout, as the verified caller;
5. its result matches the tool's output schema, or the call failed;
6. the call is audited, and only then is the result returned.

A caller is told only an outcome and a safe message: never an exception's own text, an input's
value or anything about another tenant. A tool list shows only the tools the token may call.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry import metrics
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from windops_mcp.audit import AuditSink, AuditUnavailableError, ToolCall
from windops_mcp.caller import caller_scope, current_caller
from windops_mcp.limits import LimitReachedError, Limits, LimitsUnavailableError
from windops_mcp.tokens import TokenRefusedError, ToolCaller, Verifier, bearer
from windops_mcp.tools import Tool, ToolRefusedError, ToolUnavailableError

PATH = "/mcp"
METHODS = ("POST", "GET", "DELETE")
# A tool's arguments are small. Anything larger is refused before it is parsed.
MAXIMUM_REQUEST_BYTES = 64 * 1024
# Shown to the caller for each outcome, with nothing from the failure itself.
MESSAGES = {
    "denied": "This token may not call this tool",
    "invalid": "The arguments do not match the tool's input schema",
    "limited": "Too many calls. Try again later",
    "timed_out": "The tool did not finish in time",
    "unavailable": "The tool cannot be used right now. Try again later",
    "failed": "The tool failed",
}
_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

logger = logging.getLogger("windops.mcp")
# The SDK warns that a tool it has not listed goes unvalidated. Here every tool is validated
# against its own model, and a tool missing from a caller's list is one it may not call.
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.ERROR)


def _error(outcome: str, message: str | None = None, **detail: Any) -> types.CallToolResult:
    body = {"error": outcome, "message": message or MESSAGES[outcome], **detail}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(body))], isError=True
    )


def _fields(error: ValidationError) -> list[str]:
    """Which fields were wrong, and how, but never what they held."""
    found = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(part) for part in item["loc"]) or "(arguments)"
        found.append(f"{where}: {item['msg']}"[:200])
    return found[:10]


class ToolServer:
    def __init__(
        self,
        *,
        server: str,
        audience: str,
        tools: Sequence[Tool],
        verifier: Verifier,
        limits: Limits,
        audit: AuditSink,
        version: str = "1",
        instructions: str = "",
        allowed_hosts: Sequence[str] = (),
    ) -> None:
        if any(tool.server != server for tool in tools):
            raise ValueError("Every tool must belong to this server")
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique")
        self.server = server
        self.audience = audience
        self._tools = {tool.name: tool for tool in tools}
        self._verifier = verifier
        self._limits = limits
        self._audit = audit
        self._allowed_hosts = list(allowed_hosts)
        self._duration = metrics.get_meter("windops.mcp").create_histogram(
            "windops.mcp.tool.duration",
            unit="s",
            description="Time to handle an MCP tool call, by outcome",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )
        self._mcp: Server[Any, Any] = Server(audience, version=version, instructions=instructions)
        self._mcp.list_tools()(self._list_tools)  # type: ignore[no-untyped-call]
        # Validation is done here, against the tool's own model, not by the SDK.
        self._mcp.call_tool(validate_input=False)(self._call_tool)

    def describe(self, tool: Tool) -> types.Tool:
        return types.Tool(
            name=tool.name,
            title=tool.title,
            description=tool.description,
            inputSchema=tool.input.model_json_schema(),
            outputSchema=tool.output.model_json_schema(),
            annotations=types.ToolAnnotations(
                title=tool.title,
                readOnlyHint=tool.read_only,
                destructiveHint=False,
                idempotentHint=tool.read_only,
                openWorldHint=tool.external,
            ),
        )

    async def _list_tools(self) -> list[types.Tool]:
        scopes = current_caller().scopes
        return [self.describe(tool) for tool in self._tools.values() if tool.scope in scopes]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        caller = current_caller()
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        tool = self._tools.get(name)
        if tool is None or tool.scope not in caller.scopes:
            # An unknown tool and a forbidden one look the same, so names cannot be probed.
            result, outcome = _error("denied"), "denied"
        else:
            result, outcome = await self._run(tool, caller, arguments, request_id)
        if tool is not None:
            try:
                await self._audit.record(
                    ToolCall(self.server, tool.name, caller, outcome, request_id)
                )
            except AuditUnavailableError:
                logger.error("A tool call could not be audited", extra={"tool": tool.name})
                if outcome == "succeeded":
                    result, outcome = _error("unavailable"), "unavailable"
        seconds = time.perf_counter() - started
        attributes = {"server": self.server, "tool": tool.name if tool else "unknown"}
        self._duration.record(seconds, {**attributes, "outcome": outcome})
        logger.info(
            "Tool call",
            extra={**attributes, "outcome": outcome, "seconds": round(seconds, 3),
                   "request_id": request_id},
        )  # fmt: skip
        return result

    async def _run(
        self, tool: Tool, caller: ToolCaller, arguments: dict[str, Any], request_id: str
    ) -> tuple[types.CallToolResult, str]:
        try:
            parsed = tool.input.model_validate(arguments)
        except ValidationError as error:
            return _error("invalid", fields=_fields(error)), "invalid"
        try:
            await self._limits.admit(caller, tool)
        except LimitReachedError as error:
            return _error("limited", limit=error.which), "limited"
        except LimitsUnavailableError:
            return _error("unavailable"), "unavailable"
        try:
            async with asyncio.timeout(tool.timeout_seconds):
                produced = await tool.handler(parsed)
        except TimeoutError:
            return _error("timed_out"), "timed_out"
        except ToolRefusedError as error:
            return _error("refused", error.message[:500]), "refused"
        except ToolUnavailableError:
            return _error("unavailable"), "unavailable"
        except Exception:
            logger.exception("A tool failed", extra={"tool": tool.name, "request_id": request_id})
            return _error("failed"), "failed"
        try:
            dumped = (
                produced.model_dump(mode="json") if isinstance(produced, tool.output) else produced
            )
            structured = tool.output.model_validate(dumped).model_dump(mode="json")
        except ValidationError:
            logger.error(
                "A tool's result broke its output schema",
                extra={"tool": tool.name, "request_id": request_id},
            )
            return _error("failed"), "failed"
        text = json.dumps(structured, ensure_ascii=False)
        return (
            types.CallToolResult(
                content=[types.TextContent(type="text", text=text)],
                structuredContent=structured,
                isError=False,
            ),
            "succeeded",
        )

    def _manager(self) -> StreamableHTTPSessionManager:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(self._allowed_hosts),
            allowed_hosts=self._allowed_hosts,
            allowed_origins=[],
        )
        return StreamableHTTPSessionManager(
            app=self._mcp,
            stateless=True,
            json_response=True,
            security_settings=security,
            max_request_body_size=MAXIMUM_REQUEST_BYTES,
        )

    def parts(self) -> tuple["_Authenticated", StreamableHTTPSessionManager]:
        """The endpoint, behind token checks, and the session manager that must run beside it."""
        manager = self._manager()
        return _Authenticated(manager, self._verifier), manager

    def app(self) -> Starlette:
        """A whole ASGI app, for tests and checks: the endpoint, and its manager's lifespan."""
        endpoint, manager = self.parts()

        @asynccontextmanager
        async def lifespan(_: Starlette) -> AsyncIterator[None]:
            async with manager.run():
                yield

        return Starlette(
            routes=[Route(PATH, endpoint=endpoint, methods=list(METHODS))],
            lifespan=lifespan,
        )


def _refusal(error: TokenRefusedError) -> Response:
    status = 503 if error.reason == "keys_unavailable" else 401
    headers = {}
    if status == 401:
        headers["WWW-Authenticate"] = (
            f'Bearer error="invalid_token", error_description="{error.message}"'
        )
    return JSONResponse(
        {"error": error.reason, "message": error.message}, status_code=status, headers=headers
    )


class _Authenticated:
    """Verifies the bearer token on every request, then hands it on as the verified caller."""

    def __init__(self, manager: StreamableHTTPSessionManager, verifier: Verifier) -> None:
        self._manager = manager
        self._verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        length = request.headers.get("content-length", "0")
        if not length.isdigit() or int(length) > MAXIMUM_REQUEST_BYTES:
            await JSONResponse(
                {"error": "too_large", "message": "The request is too large"}, status_code=413
            )(scope, receive, send)
            return
        try:
            caller = await self._verifier.verify(bearer(request.headers.get("authorization")))
        except TokenRefusedError as error:
            await _refusal(error)(scope, receive, send)
            return
        with caller_scope(caller):
            await self._manager.handle_request(scope, receive, send)
