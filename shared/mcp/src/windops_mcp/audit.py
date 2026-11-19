"""An audit record for every tool call a token got through to, whatever its outcome (MCP-003).

A record names the tool, who called it and the outcome, never the arguments or the result. It
goes to the Audit Service as an `AuditRequested` event. A call that succeeds is audited before its
result is returned: if the record cannot be sent, the result is withheld.
"""

from dataclasses import dataclass
from typing import Protocol

from windops.events.v1 import events_pb2
from windops_events import audit_request_subject
from windops_events.backend import Publisher

from windops_mcp.tokens import ToolCaller

OUTCOMES = frozenset(
    {"succeeded", "denied", "invalid", "limited", "refused", "timed_out", "unavailable", "failed"}
)


class AuditUnavailableError(Exception):
    """The record could not be sent."""


@dataclass(frozen=True)
class ToolCall:
    server: str
    tool: str
    caller: ToolCaller
    outcome: str
    request_id: str

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"Unknown outcome: {self.outcome}")


class AuditSink(Protocol):
    async def record(self, call: ToolCall) -> None:
        """Send the record, or raise AuditUnavailableError."""


def audit_event(call: ToolCall) -> events_pb2.AuditRequested:
    """An agent acting for a user is the actor, on the user's behalf (SEC-010)."""
    caller = call.caller
    return events_pb2.AuditRequested(
        action=f"mcp.{call.server}.{call.tool}",
        actor_type="agent" if caller.agent else "user",
        actor_id=caller.agent or caller.user_id,
        on_behalf_of=caller.user_id if caller.agent else "",
        resource_type="mcp_tool",
        resource_id=f"{call.server}/{call.tool}",
        outcome=call.outcome,
        # The run's ID for an agent's call, shared with its trace (OBS-008); else this call's.
        correlation_id=caller.correlation_id or call.request_id,
    )


class EventsAudit:
    """Publishes to the audit stream, under this server's own subject."""

    def __init__(self, publisher: Publisher, *, producer: str) -> None:
        self._publisher = publisher
        self._subject = audit_request_subject(producer)

    async def record(self, call: ToolCall) -> None:
        try:
            await self._publisher.publish(
                self._subject,
                tenant_id=call.caller.tenant_id,
                payload=audit_event(call),
                request_id=call.request_id,
            )
        except Exception as error:
            raise AuditUnavailableError("The audit record could not be sent") from error


class MemoryAudit:
    """Keeps the records, for tests and a server run by hand."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []
        self.fail = False

    async def record(self, call: ToolCall) -> None:
        if self.fail:
            raise AuditUnavailableError("The audit record could not be sent")
        self.calls.append(call)
