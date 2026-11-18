"""The PostgreSQL MCP: fixed statistics queries, no query text, for Platform Administrators."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from windops_mcp import ToolRefusedError, ToolUnavailableError
from windops_mcp_postgres.tools import CONNECTIONS, OVERVIEW, Nothing, database, tools


class Fake:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    async def require(self, permission: str) -> None:
        if not self.allowed:
            raise ToolRefusedError(f"This needs {permission}")


def test_the_queries_read_only_statistics_and_never_a_querys_text() -> None:
    for query in (str(OVERVIEW), str(CONNECTIONS)):
        lowered = query.lower()
        assert "pg_stat_" in lowered
        for forbidden in (" query,", "query ", "client_addr", "usename", "application_name",
                          "insert", "update", "delete"):  # fmt: skip
            assert forbidden not in lowered.replace("query_start", "")
    assert "windops\\_%" in str(OVERVIEW)


def test_a_cache_ratio_is_given_only_when_there_were_reads() -> None:
    row = SimpleNamespace(
        database="windops_asset", size_bytes=10, connections=2, commits=5, rollbacks=0,
        deadlocks=0, temp_bytes=0, blks_hit=90, blks_read=10,
    )  # fmt: skip
    assert database(row).cache_hit_ratio == 0.9
    row.blks_hit = row.blks_read = 0
    assert database(row).cache_hit_ratio is None


class Broken:
    def connect(self) -> Any:
        raise OSError("down")


@pytest.mark.parametrize("name", ["database_overview", "connection_activity"])
def test_without_the_permission_nothing_is_queried(name: str) -> None:
    tool = {t.name: t for t in tools(Fake(allowed=False), Broken())}[name]  # type: ignore[arg-type]
    with pytest.raises(ToolRefusedError, match=r"evaluation\.manage"):
        asyncio.run(tool.handler(Nothing()))


def test_an_unreachable_server_is_unavailable() -> None:
    tool = tools(Fake(), Broken())[0]  # type: ignore[arg-type]
    with pytest.raises(ToolUnavailableError):
        asyncio.run(tool.handler(Nothing()))
