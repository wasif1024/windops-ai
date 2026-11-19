"""Agents' tool tokens, from the Identity Service, under the run's delegation (M8.5, ADR 0019).

The runtime asks as itself, by its certificate, in the run's tenant. Each token is for one agent,
one MCP server and named tools, and lasts 120 seconds. It goes only into the Authorization header
of a tool call: never into a prompt, a tool result, a checkpoint or a log (SEC-008).
"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol

import grpc
from windops.identity.v1 import identity_pb2
from windops_grpc import ServiceClient, UnavailableError, acting_as_service
from windops_resilience import BreakerOpenError

from windops_agent_runtime.store import SERVICE_NAME

DEADLINE_SECONDS = 5.0


class NoDelegationError(Exception):
    """The run has no delegation, so its agents have no one to act for."""


class Identity(Protocol):
    async def agent_token(
        self,
        tenant: str,
        delegation: str,
        agent: str,
        audience: str,
        scopes: Sequence[str],
        correlation_id: str = "",
    ) -> str: ...

    async def end(self, tenant: str, delegation: str, reason: str) -> None: ...


class IdentityClient:
    def __init__(self, stub: Callable[[], Any], client: ServiceClient, secret: str) -> None:
        self._stub, self._client, self._secret = stub, client, secret

    async def agent_token(
        self,
        tenant: str,
        delegation: str,
        agent: str,
        audience: str,
        scopes: Sequence[str],
        correlation_id: str = "",
    ) -> str:
        request = identity_pb2.IssueAgentTokenRequest(
            delegation_id=delegation,
            agent=agent,
            audience=audience,
            scopes=list(scopes),
            correlation_id=correlation_id,
        )
        with acting_as_service(SERVICE_NAME, tenant, self._secret):
            try:
                answer = await self._client.call(
                    self._stub().IssueAgentToken, request, idempotent=True,
                    deadline_seconds=DEADLINE_SECONDS,
                )  # fmt: skip
            except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
                raise UnavailableError("No agent token could be had") from error
        return str(answer.access_token)

    async def end(self, tenant: str, delegation: str, reason: str) -> None:
        request = identity_pb2.EndDelegationRequest(
            delegation_id=delegation, reason=reason[:500], idempotency_key=f"end-{delegation}"
        )
        with acting_as_service(SERVICE_NAME, tenant, self._secret):
            await self._client.call(
                self._stub().EndDelegation, request, idempotent=True,
                deadline_seconds=DEADLINE_SECONDS,
            )  # fmt: skip


class AgentTokens:
    """A run's view: tokens for its agents, under its own delegation and tenant only."""

    def __init__(
        self, identity: Identity, tenant: str, delegation: str, correlation_id: str = ""
    ) -> None:
        self._identity, self._tenant, self._delegation = identity, tenant, delegation
        # The run's ID, so each tool call's audit record shares it with the trace (OBS-008).
        self._correlation = correlation_id

    async def for_tool(self, agent: str, audience: str, scopes: Sequence[str]) -> str:
        if not self._delegation:
            raise NoDelegationError("The run has no delegation for its agents to act under")
        return await self._identity.agent_token(
            self._tenant, self._delegation, agent, audience, scopes, self._correlation
        )
