"""A tool definition that is unsafe cannot be made, so no server can start with one (MCP-003)."""

from typing import Annotated, Any

import pytest
from pydantic import ConfigDict, Field
from windops_mcp import Tool, ToolInput, ToolOutput, Untrusted, tool_scope


class Query(ToolInput):
    query: str = Field(min_length=1, max_length=400)
    limit: int = Field(default=5, ge=1, le=20)
    kinds: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=5)


class Plain(ToolOutput):
    count: int


class Wrapped(ToolOutput):
    items: list[Untrusted]


async def handle(_: Any) -> ToolOutput:
    return Plain(count=0)


def make(**changes: Any) -> Tool:
    options: dict[str, Any] = {
        "server": "documentation",
        "name": "search_documents",
        "title": "Search documents",
        "description": "Search approved documents.",
        "input": Query,
        "output": Plain,
        "handler": handle,
    }
    options.update(changes)
    return Tool(**options)


def test_a_tools_scope_names_its_server_and_itself() -> None:
    assert make().scope == "mcp.documentation.search_documents"
    assert tool_scope("weather", "get_forecast") == "mcp.weather.get_forecast"


class Unbounded(ToolInput):
    query: str


class UnboundedList(ToolInput):
    ids: list[Annotated[str, Field(max_length=10)]]


class UnboundedInList(ToolInput):
    ids: list[str] = Field(max_length=3)


class Nested(ToolInput):
    inner: Unbounded


class Lenient(ToolInput):
    model_config = ConfigDict(extra="ignore", frozen=True)
    query: str = Field(max_length=5)


class OpenMap(ToolInput):
    tags: dict[str, Annotated[str, Field(max_length=5)]]


@pytest.mark.parametrize(
    ("schema", "complaint"),
    [
        (Unbounded, "no size limit on query"),
        (UnboundedList, "no size limit on ids"),
        (UnboundedInList, r"no size limit on ids\[\]"),
        (Nested, "no size limit on inner.query"),
        (Lenient, "refuse unknown fields"),
        (OpenMap, "no size limit on tags"),
    ],
)
def test_an_input_without_limits_is_refused(schema: type[ToolInput], complaint: str) -> None:
    with pytest.raises(ValueError, match=complaint):
        make(input=schema)


def test_a_tool_returning_external_content_must_wrap_it() -> None:
    with pytest.raises(ValueError, match="Untrusted"):
        make(external=True)
    assert make(external=True, output=Wrapped).external


def test_a_tool_returning_external_content_cannot_write() -> None:
    """MCP-006 starts here: reading the outside world and changing things never mix."""
    with pytest.raises(ValueError, match="only reads"):
        make(external=True, output=Wrapped, read_only=False)


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Search"},
        {"name": "search documents"},
        {"server": "docs-server"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"per_minute": 0},
        {"per_minute": 10, "per_day": 5},
        {"description": " "},
        {"description": "x" * 2001},
    ],
)
def test_names_timeouts_limits_and_descriptions_are_checked(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make(**changes)


def test_schemas_must_be_the_libraries_own() -> None:
    from pydantic import BaseModel

    class Loose(BaseModel):
        query: str = Field(max_length=5)

    with pytest.raises(TypeError):
        make(input=Loose)
