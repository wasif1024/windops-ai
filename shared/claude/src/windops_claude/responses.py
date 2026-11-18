"""Check every response before its content is read (LLM-005), and note any fallback (LLM-006).

Only `end_turn`, `tool_use` and `stop_sequence` let a caller read on. Anything else raises:

| stop_reason | Raised | Why |
| --- | --- | --- |
| `refusal` | RefusedError | Claude, and any fallback model, declined. Never read as an answer |
| `max_tokens` | TruncatedError | The answer is cut off, so it cannot be trusted as complete |
| `pause_turn` | UnexpectedStopError | Only server tools pause a turn, and agents use none |
| anything else | UnexpectedStopError | A new stop reason is reviewed before it is accepted |
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage

from windops_claude.pricing import Usage

READABLE = frozenset({"end_turn", "tool_use", "stop_sequence"})


class StopError(Exception):
    def __init__(self, message: str, response: "Response") -> None:
        super().__init__(message)
        self.response = response


class RefusedError(StopError):
    """Claude declined, after any fallback. The caller records it and carries on without it."""


class TruncatedError(StopError):
    """The output limit cut the answer off."""


class UnexpectedStopError(StopError):
    """A stop reason this code does not accept."""


@dataclass(frozen=True)
class Fallback:
    """A model declined, and another answered (LLM-006). Kept in the run's record and trace."""

    from_model: str
    to_model: str


@dataclass(frozen=True)
class Response:
    requested_model: str
    served_model: str
    stop_reason: str
    stop_details: Mapping[str, Any]
    usage: Usage
    fallbacks: tuple[Fallback, ...]

    @property
    def fell_back(self) -> bool:
        return bool(self.fallbacks) or self.served_model != self.requested_model


def _fallbacks(message: AIMessage, usage: Mapping[str, Any]) -> tuple[Fallback, ...]:
    found = []
    if isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, dict) and block.get("type") == "fallback":
                found.append(
                    Fallback(
                        from_model=str((block.get("from") or {}).get("model", "")),
                        to_model=str((block.get("to") or {}).get("model", "")),
                    )
                )
    return tuple(found)


def read(message: AIMessage, requested_model: str) -> Response:
    """What a response says about itself, without reading its content."""
    metadata = message.response_metadata
    usage = metadata.get("usage") or {}
    if not isinstance(usage, Mapping):
        usage = dict(usage)
    served = str(metadata.get("model") or metadata.get("model_name") or requested_model)
    fallbacks = _fallbacks(message, usage)
    if not fallbacks and any(
        isinstance(entry, Mapping) and entry.get("type") == "fallback_message"
        for entry in usage.get("iterations") or ()
    ):
        # A sticky fallback can serve a turn with no block of its own: usage still says so.
        fallbacks = (Fallback(from_model=requested_model, to_model=served),)
    return Response(
        requested_model=requested_model,
        served_model=served,
        stop_reason=str(metadata.get("stop_reason") or ""),
        stop_details=dict(metadata.get("stop_details") or {}),
        usage=Usage.from_api(usage),
        fallbacks=fallbacks,
    )


def check(message: AIMessage, requested_model: str) -> Response:
    """Return the response's record if its content may be read, or raise."""
    response = read(message, requested_model)
    reason = response.stop_reason
    if reason in READABLE:
        return response
    if reason == "refusal":
        category = response.stop_details.get("category", "unstated")
        raise RefusedError(f"Claude declined ({category})", response)
    if reason == "max_tokens":
        raise TruncatedError("The output limit cut the answer off", response)
    raise UnexpectedStopError(f"Unexpected stop reason: {reason or 'none'}", response)
