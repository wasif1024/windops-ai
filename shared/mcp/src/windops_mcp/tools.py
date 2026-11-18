"""A tool's definition: its scope, its schemas, its limits and its handler (MCP-003).

A definition is checked when it is made, so a server cannot start with a tool that is unsafe:

- its input refuses unknown fields, and bounds every string's length and every list's size;
- its output is a schema too, and the server checks every result against it;
- a tool that returns content from outside the platform must return it as `Untrusted`;
- its scope is `mcp.<server>.<tool>`, the scope its token must carry;
- it has a timeout, a per-caller rate and a per-tenant daily quota.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

MAXIMUM_TIMEOUT_SECONDS = 60.0
_NAME = re.compile(r"[a-z][a-z0-9_]{1,62}")
_SERVER = re.compile(r"[a-z][a-z0-9]{1,30}")


class ToolInput(BaseModel):
    """A tool's arguments. Unknown fields are refused, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolOutput(BaseModel):
    """A tool's result. Nothing leaves that its schema does not name."""

    model_config = ConfigDict(extra="forbid")


Handler = Callable[[Any], Awaitable[ToolOutput]]


class ToolRefusedError(Exception):
    """The tool will not do this, for a reason the caller may read, such as no such record."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ToolUnavailableError(Exception):
    """What the tool depends on cannot be reached now. The caller may try again later."""


def tool_scope(server: str, tool: str) -> str:
    return f"mcp.{server}.{tool}"


def _unbounded(schema: Any, definitions: dict[str, Any], path: str, seen: set[str]) -> list[str]:
    """The places in a JSON schema where a string or a list has no size limit."""
    if not isinstance(schema, dict):
        return []
    found: list[str] = []
    reference = schema.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        if name in seen:
            return []
        return _unbounded(definitions.get(name, {}), definitions, path, seen | {name})
    kind = schema.get("type")
    limited = "enum" in schema or "const" in schema
    if kind == "string" and not limited and "maxLength" not in schema:
        found.append(path or "(input)")
    if kind == "array" and "maxItems" not in schema:
        found.append(path or "(input)")
    if kind == "object" and schema.get("additionalProperties", False) not in (False, None):
        found.append(path or "(input)")
    for key, value in schema.get("properties", {}).items():
        found += _unbounded(value, definitions, f"{path}.{key}" if path else key, seen)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            found += _unbounded(schema[key], definitions, f"{path}[]", seen)
    for key in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(key, []):
            found += _unbounded(option, definitions, path, seen)
    return found


def _mentions_untrusted(schema: dict[str, Any]) -> bool:
    return "Untrusted" in schema.get("$defs", {})


@dataclass(frozen=True)
class Tool:
    server: str
    name: str
    title: str
    description: str
    input: type[ToolInput]
    output: type[ToolOutput]
    handler: Handler
    timeout_seconds: float = 10.0
    # Calls each caller may make in a minute, and each tenant in a day.
    per_minute: int = 30
    per_day: int = 2000
    # Whether it returns content from outside the platform: web pages, documents or files.
    external: bool = False
    # Whether it only reads. A tool that changes anything says so, for the allowlists (MCP-006).
    read_only: bool = True

    def __post_init__(self) -> None:
        if not _SERVER.fullmatch(self.server) or not _NAME.fullmatch(self.name):
            raise ValueError(f"Unsupported tool name: {self.server}.{self.name}")
        if not issubclass(self.input, ToolInput) or not issubclass(self.output, ToolOutput):
            raise TypeError("A tool's schemas are a ToolInput and a ToolOutput")
        if self.input.model_config.get("extra") != "forbid":
            raise ValueError(f"{self.name}: its input must refuse unknown fields")
        schema = self.input.model_json_schema()
        unbounded = _unbounded(schema, schema.get("$defs", {}), "", set())
        if unbounded:
            raise ValueError(f"{self.name}: no size limit on {', '.join(unbounded)}")
        if self.external and not self.read_only:
            raise ValueError(f"{self.name}: a tool that returns external content only reads")
        if self.external and not _mentions_untrusted(self.output.model_json_schema()):
            raise ValueError(f"{self.name}: external content must be returned as Untrusted")
        if not 0 < self.timeout_seconds <= MAXIMUM_TIMEOUT_SECONDS:
            raise ValueError(f"{self.name}: a timeout is up to {MAXIMUM_TIMEOUT_SECONDS:g}s")
        if self.per_minute < 1 or self.per_day < self.per_minute:
            raise ValueError(f"{self.name}: limits are at least 1, and a day's at least a minute's")
        if not self.description.strip() or len(self.description) > 2000:
            raise ValueError(f"{self.name}: a description is needed, of at most 2000 characters")

    @property
    def scope(self) -> str:
        return tool_scope(self.server, self.name)
