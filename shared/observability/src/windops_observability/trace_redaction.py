"""What an agent trace may carry out of the cluster to LangSmith (EGR-003, OBS-003, LLM-009).

Every input, output and metadata value of every run goes through `redact_trace` before export.
It runs inside the LangSmith client, so nothing can be exported unredacted. It removes:

- **Secrets:** passwords, keys, tokens, private keys and credentials, by key and by value
  (`windops_observability.redaction`). A tool's bearer token never reaches a trace.
- **Personal data:** e-mail addresses anywhere, and the fields that name people: names, display
  names, usernames, phone numbers. IDs stay, as OBS-005 needs them: they name no one.
- **Private reasoning:** Claude's thinking blocks, signed or redacted, wherever they appear,
  including inside LangChain's serialized messages (LLM-009). The trace keeps that one was there.
- **Bulk text:** any string over 4,000 characters is cut, with its length noted. A trace never
  carries a whole document, web page or evidence item out of the cluster.

Objects, such as the LangChain messages a run's inputs and outputs hold, are taken apart and
redacted field by field: nothing reaches the exporter still holding what it would serialize.

Leakage tests plant each of these in a real trace, and scan what LangSmith received (M8.9).
"""

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from windops_observability.redaction import REDACTED, _is_sensitive_key, redact_text

MAXIMUM_TEXT = 4000
PRIVATE_BLOCKS = frozenset({"thinking", "redacted_thinking"})
# Keys that hold a person's name or contact, whatever their value looks like.
_PERSONAL_KEYS = frozenset(
    {
        "email", "e_mail", "display_name", "displayname", "first_name", "firstname",
        "last_name", "lastname", "full_name", "fullname", "given_name", "family_name",
        "username", "user_name", "phone", "phone_number", "mobile",
    }
)  # fmt: skip
# Thinking's own fields, wherever a block is unpacked.
_PRIVATE_KEYS = frozenset({"thinking", "redacted_thinking", "signature"})


def _personal(key: str) -> bool:
    return key.lower().replace("-", "_") in _PERSONAL_KEYS


def _text(text: str) -> str:
    text = redact_text(text)
    if len(text) > MAXIMUM_TEXT:
        return f"{text[:MAXIMUM_TEXT]}… [cut: {len(text)} characters]"
    return text


def redact_trace(value: Any) -> Any:
    """A copy of a run's inputs, outputs or metadata, safe to export."""
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, Mapping):
        if value.get("type") in PRIVATE_BLOCKS:
            # Kept as a marker, so a trace shows the model thought, never what.
            return {"type": value["type"], "omitted": True}
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _is_sensitive_key(name) or _personal(name):
                cleaned[name] = REDACTED
            elif name in _PRIVATE_KEYS and isinstance(item, str):
                cleaned[name] = "[omitted]"
            else:
                cleaned[name] = redact_trace(item)
        return cleaned
    if isinstance(value, list | tuple):
        return [redact_trace(item) for item in value]
    if isinstance(value, set | frozenset):
        return [redact_trace(item) for item in sorted(value, key=str)]
    if value is None or isinstance(value, bool | int | float | Decimal | UUID | date | Enum):
        return value
    # An object, such as a LangChain message a tool returned: its fields, redacted the same way.
    # Never passed through as it is, or the exporter would serialize what it holds unredacted.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return redact_trace(dump())
    if is_dataclass(value) and not isinstance(value, type):
        return redact_trace(asdict(value))
    return _text(str(value))
