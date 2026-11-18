"""Prompt caching, append-only history and private reasoning (LLM-007, LLM-008, LLM-009).

- **Caching:** a stable system prompt goes first, marked for the cache. Anything that changes
  between calls, such as a time, a date or a record's ID, is refused there: it belongs in a
  message after the breakpoint, or every call would miss the cache.
- **History:** each request of a conversation must repeat the previous request and its response
  exactly, then add to them. Thinking blocks are replayed byte for byte. `HistoryGuard` refuses a
  request that edits, drops or reorders anything.
- **Private reasoning:** `public_content` drops thinking blocks from anything kept or shown, such
  as an audit record or an explanation. They stay only in the conversation itself.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage

PRIVATE_BLOCKS = frozenset({"thinking", "redacted_thinking"})
_VOLATILE = (
    (re.compile(r"\b\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?"), "a date or time"),
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"), "a time of day"),
    (re.compile(r"\b(case|evs|run|res|req)_[a-z0-9]{8,}\b"), "a record's ID"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "a UUID"),
)


class VolatilePromptError(ValueError):
    """The stable prompt holds something that changes between calls."""


class HistoryEditedError(ValueError):
    """A request did not repeat the conversation so far exactly (LLM-008)."""


def check_stable(text: str) -> None:
    for pattern, what in _VOLATILE:
        if pattern.search(text):
            raise VolatilePromptError(
                f"The stable prompt holds {what}: put it after the cache breakpoint"
            )


def stable_system(text: str) -> SystemMessage:
    """The system prompt, marked for the cache. Only text that never changes may go here."""
    check_stable(text)
    return SystemMessage(
        content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    )


def public_content(message: BaseMessage) -> str | list[Any]:
    """The message's content, without private reasoning (LLM-009)."""
    if isinstance(message.content, str):
        return message.content
    return [
        block
        for block in message.content
        if not (isinstance(block, dict) and block.get("type") in PRIVATE_BLOCKS)
    ]


def _fingerprint(message: BaseMessage) -> str:
    # The type and content are what reach Claude. IDs and metadata do not, and may differ.
    return json.dumps(
        {
            "type": message.type,
            "content": message.content,
            "tool_calls": getattr(message, "tool_calls", None),
        },
        sort_keys=True,
        default=str,
    )


class HistoryGuard:
    """Holds one conversation's history as sent, and refuses any request that edits it."""

    def __init__(self) -> None:
        self._sent: list[str] = []

    def before(self, messages: Sequence[BaseMessage]) -> None:
        current = [_fingerprint(m) for m in messages]
        if current[: len(self._sent)] != self._sent:
            for index, (was, now) in enumerate(zip(self._sent, current, strict=False)):
                if was != now:
                    raise HistoryEditedError(f"Message {index} differs from what was sent before")
            raise HistoryEditedError("The request drops messages that were sent before")
        self._sent = current

    def after(self, response: BaseMessage) -> None:
        self._sent.append(_fingerprint(response))
