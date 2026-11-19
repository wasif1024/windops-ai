"""Run an MCP server as a platform workload, and call domain services for its caller.

The MCP endpoint is served only over HTTPS, and only to a client presenting a certificate from the
cluster's own CA: the Agent Runtime, or a check holding one (MCP-001). Plain HTTP carries only
health and metrics, for probes and Prometheus.

A server's settings are its Keycloak issuer and keys, its own audience, its Redis login for limits
and its NATS login for audit records, each from its Secrets, never from .env.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import grpc
from fastapi import APIRouter
from opentelemetry import metrics
from starlette.routing import Route
from windops.identity.v1 import identity_pb2, identity_pb2_grpc
from windops_auth import (
    IDENTITY_METADATA_KEY,
    VerifiedIdentity,
    current_identity_token,
    sign_identity,
)
from windops_cache import cache_breaker
from windops_cache import connect as redis_connect
from windops_events import open_events
from windops_events.backend import Events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_observability import breaker_state_metrics
from windops_resilience import BreakerConfig, BreakerOpenError, CircuitBreaker

from windops_mcp.audit import AuditUnavailableError, EventsAudit, ToolCall
from windops_mcp.caller import acting_for_caller
from windops_mcp.limits import RedisLimits
from windops_mcp.server import METHODS, PATH, ToolServer
from windops_mcp.tokens import AgentIssuer, TokenSettings, TokenVerifier
from windops_mcp.tools import Tool, ToolRefusedError, ToolUnavailableError

DEADLINE_SECONDS = 5.0
# The platform's own tenant, for calls a server makes as itself, about no tenant's data.
PLATFORM_TENANT = "windops-platform"
# What a service's refusal is passed on as. Its own message goes too: services write their
# refusals for callers to read.
_REFUSED = {
    grpc.StatusCode.INVALID_ARGUMENT: "",
    grpc.StatusCode.FAILED_PRECONDITION: "",
    grpc.StatusCode.NOT_FOUND: "No such record, or not one the caller may see",
    grpc.StatusCode.PERMISSION_DENIED: "The caller may not do this",
    grpc.StatusCode.UNAUTHENTICATED: "The caller's identity was not accepted",
    grpc.StatusCode.RESOURCE_EXHAUSTED: "A limit was reached. Try again later",
    grpc.StatusCode.ALREADY_EXISTS: "",
}


def server_name(server: str) -> str:
    """The workload's name, and its Redis key prefix and audit subject: mcp-documentation."""
    return f"mcp-{server}"


class Services:
    """Calls domain services as the current tool caller, through a breaker for each."""

    def __init__(self, tls: Path, secret: str, environ: dict[str, str] | None = None) -> None:
        self._tls, self._secret = tls, secret
        self._env = dict(os.environ if environ is None else environ)
        self._stubs: dict[str, Any] = {}
        self._clients: dict[str, ServiceClient] = {}

    def _stub(self, dependency: str, make: Any, setting: str) -> tuple[Any, ServiceClient]:
        # A channel must be made on the running loop, so on the first call.
        if dependency not in self._stubs:
            self._stubs[dependency] = make(create_channel(self._env[setting], self._tls))
            breaker = CircuitBreaker(
                dependency,
                BreakerConfig(slow_call_seconds=2.0),
                on_state_change=breaker_state_metrics(metrics.get_meter_provider()),
            )
            self._clients[dependency] = ServiceClient(
                breaker, identity_token=current_identity_token
            )
        return self._stubs[dependency], self._clients[dependency]

    async def call(
        self,
        dependency: str,
        make: Any,
        setting: str,
        method: str,
        request: Any,
        *,
        idempotent: bool = True,
        deadline_seconds: float = DEADLINE_SECONDS,
    ) -> Any:
        stub, client = self._stub(dependency, make, setting)
        with acting_for_caller(self._secret):
            try:
                return await client.call(
                    getattr(stub, method), request, idempotent=idempotent,
                    deadline_seconds=deadline_seconds,
                )  # fmt: skip
            except grpc.aio.AioRpcError as error:
                if error.code() in _REFUSED:
                    message = _REFUSED[error.code()] or (error.details() or "Refused")[:300]
                    raise ToolRefusedError(message) from error
                raise ToolUnavailableError(f"{dependency} cannot be reached") from error
            except (BreakerOpenError, TimeoutError) as error:
                raise ToolUnavailableError(f"{dependency} cannot be reached") from error

    async def require(self, permission: str) -> None:
        """Refuse the call unless the Authorization Service lets the caller do this."""
        from windops.authorization.v1 import authorization_pb2, authorization_pb2_grpc

        answer = await self.call(
            "authorization", authorization_pb2_grpc.AuthorizationServiceStub,
            "WINDOPS_AUTHORIZATION_ADDRESS", "Check",
            authorization_pb2.CheckRequest(permission=permission),
        )  # fmt: skip
        if not answer.allowed:
            raise ToolRefusedError(f"This needs {permission}")


class StreamAudit:
    """Sends records to the audit stream, connecting on first use."""

    def __init__(self, producer: str) -> None:
        self._producer = producer
        self._events: Events | None = None
        self._lock = asyncio.Lock()

    async def record(self, call: ToolCall) -> None:
        async with self._lock:
            if self._events is None:
                try:
                    self._events = await open_events(self._producer)
                except Exception as error:
                    raise AuditUnavailableError("The audit stream cannot be reached") from error
        await EventsAudit(self._events.publisher, producer=self._producer).record(call)


def agent_keys(
    address: str, tls: Path, secret: str, name: str
) -> Callable[[], Awaitable[list[dict[str, Any]]]]:
    """Fetch the keys agent tokens are signed with, from the Identity Service, over mutual TLS:
    nothing in between can swap them."""
    stubs: list[Any] = []

    async def fetch() -> list[dict[str, Any]]:
        if not stubs:
            # A channel must be made on the running loop, so on the first call.
            stubs.append(identity_pb2_grpc.IdentityServiceStub(create_channel(address, tls)))
        own = VerifiedIdentity(subject=f"service:{name}", tenant_id=PLATFORM_TENANT)
        metadata = ((IDENTITY_METADATA_KEY, sign_identity(own, secret, ttl_seconds=60)),)
        answer = await stubs[0].GetAgentTokenKeys(
            identity_pb2.GetAgentTokenKeysRequest(), metadata=metadata, timeout=5
        )
        keys: list[dict[str, Any]] = json.loads(answer.jwks_json)["keys"]
        return keys

    return fetch


def build(
    server: str,
    make_tools: Callable[[Services], Sequence[Tool]],
    *,
    instructions: str,
    environ: dict[str, str] | None = None,
) -> ToolServer:
    env = dict(os.environ if environ is None else environ)
    tls = Path(env["WINDOPS_TLS_DIR"])
    name = server_name(server)
    verifier = TokenVerifier(
        TokenSettings(
            issuer=env["WINDOPS_OIDC_ISSUER"],
            audience=env["WINDOPS_MCP_AUDIENCE"],
            jwks_url=env["WINDOPS_OIDC_JWKS_URL"],
            ca_file=str(tls / "ca.crt"),
        ),
        agents=AgentIssuer(
            env["WINDOPS_AGENT_TOKEN_ISSUER"],
            agent_keys(env["WINDOPS_IDENTITY_ADDRESS"], tls, env["WINDOPS_IDENTITY_SECRET"], name),
        ),
    )
    redis = redis_connect(
        host=env["WINDOPS_CACHE_HOST"],
        port=int(env["WINDOPS_CACHE_PORT"]),
        username=env["WINDOPS_CACHE_USERNAME"],
        password=env["WINDOPS_CACHE_PASSWORD"],
        ca_file=str(tls / "ca.crt"),
    )
    services = Services(tls, env["WINDOPS_IDENTITY_SECRET"], env)
    return ToolServer(
        server=server,
        audience=env["WINDOPS_MCP_AUDIENCE"],
        tools=make_tools(services),
        verifier=verifier,
        limits=RedisLimits(redis, service=name, breaker=cache_breaker()),
        audit=StreamAudit(name),
        version=env.get("WINDOPS_VERSION", "unknown"),
        instructions=instructions,
    )


def run(
    server: str, make_tools: Callable[[Services], Sequence[Tool]], *, instructions: str
) -> None:
    """Serve until SIGTERM, over HTTPS with client certificates, as every MCP server does."""
    tools = build(server, make_tools, instructions=instructions)
    endpoint, manager = tools.parts()
    router = APIRouter()
    router.routes.append(Route(PATH, endpoint=endpoint, methods=list(METHODS)))
    running = asyncio.Event()

    async def sessions(stop: asyncio.Event) -> None:
        async with manager.run():
            running.set()
            await stop.wait()

    async def ready() -> bool:
        return running.is_set()

    asyncio.run(
        run_service(
            server_name(server),
            lambda _: None,
            readiness_checks=(ready,),
            http_router=router,
            api_over_https=True,
            api_client_certificates=True,
            background=[sessions],
        )
    )
