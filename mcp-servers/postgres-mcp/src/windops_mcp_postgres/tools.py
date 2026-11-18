"""The PostgreSQL MCP's tools: read-only diagnostics of the platform's server. Development only.

It is never an agent's data path (SRS 6.3). It logs in as its own role, which holds only
`pg_monitor`: no grant on any service's database, and no table it may read. Its queries are fixed,
read only the statistics views, and never select a query's text, a client's address or anything
from a tenant's rows. The caller needs `evaluation.manage`, which only a Platform Administrator
holds. It is never deployed on Azure.
"""

from typing import Any

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops_mcp import Tool, ToolInput, ToolOutput, ToolUnavailableError
from windops_mcp.serve import Services

SERVER = "postgres"
INSTRUCTIONS = (
    "Development only: read-only diagnostics of the platform's PostgreSQL server: each "
    "service database's size and activity, and its connections by state. Never tenant data."
)
PERMISSION = "evaluation.manage"
OVERVIEW = text(
    "SELECT d.datname AS database, pg_database_size(d.datname) AS size_bytes, "
    "s.numbackends AS connections, s.xact_commit AS commits, s.xact_rollback AS rollbacks, "
    "s.deadlocks, s.temp_bytes, s.blks_hit, s.blks_read "
    "FROM pg_database d JOIN pg_stat_database s ON s.datid = d.oid "
    "WHERE d.datname LIKE 'windops\\_%' ORDER BY d.datname"
)
# No query text, user's address or application name: only counts and ages.
CONNECTIONS = text(
    "SELECT datname AS database, coalesce(state, 'unknown') AS state, count(*) AS connections, "
    "coalesce(max(extract(epoch FROM now() - query_start)), 0) AS longest_seconds, "
    "count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting_on_locks "
    "FROM pg_stat_activity WHERE datname LIKE 'windops\\_%' "
    "AND backend_type = 'client backend' "
    "GROUP BY datname, state ORDER BY datname, state"
)


class Nothing(ToolInput):
    pass


class Database(ToolOutput):
    database: str
    size_bytes: int
    connections: int
    commits: int
    rollbacks: int
    deadlocks: int
    temp_bytes: int
    cache_hit_ratio: float | None


class Overview(ToolOutput):
    databases: list[Database] = Field(max_length=100)


class Connections(ToolOutput):
    database: str
    state: str
    connections: int
    longest_seconds: float
    waiting_on_locks: int


class Activity(ToolOutput):
    connections: list[Connections]


def database(row: Any) -> Database:
    reads = row.blks_hit + row.blks_read
    return Database(
        database=row.database, size_bytes=row.size_bytes, connections=row.connections,
        commits=row.commits, rollbacks=row.rollbacks, deadlocks=row.deadlocks,
        temp_bytes=row.temp_bytes,
        cache_hit_ratio=round(row.blks_hit / reads, 4) if reads else None,
    )  # fmt: skip


def tools(services: Services, engine: AsyncEngine | None = None) -> list[Tool]:
    def connected() -> AsyncEngine:
        nonlocal engine
        if engine is None:
            from windops_migrations import create_service_engine

            engine = create_service_engine(pool_size=2, max_overflow=0, pool_pre_ping=True)
        return engine

    async def rows(query: Any) -> list[Any]:
        try:
            async with connected().connect() as connection:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                return list((await connection.execute(query)).all())
        except Exception as error:
            raise ToolUnavailableError("PostgreSQL cannot be reached") from error

    async def overview(_: Nothing) -> Overview:
        await services.require(PERMISSION)
        return Overview(databases=[database(row) for row in await rows(OVERVIEW)])

    async def activity(_: Nothing) -> Activity:
        await services.require(PERMISSION)
        return Activity(
            connections=[
                Connections(
                    database=row.database,
                    state=row.state,
                    connections=row.connections,
                    longest_seconds=round(float(row.longest_seconds), 1),
                    waiting_on_locks=row.waiting_on_locks,
                )
                for row in await rows(CONNECTIONS)
            ]
        )

    return [
        Tool(
            server=SERVER,
            name="database_overview",
            title="Database overview",
            description=(
                "Each service database's size, connections, commits, rollbacks, deadlocks, "
                "temporary bytes and cache hit ratio."
            ),
            input=Nothing,
            output=Overview,
            handler=overview,
            timeout_seconds=10,
            per_minute=10,
            per_day=500,
        ),
        Tool(
            server=SERVER,
            name="connection_activity",
            title="Connection activity",
            description=(
                "Connections to each service database by state, the longest running one's age "
                "and how many wait on locks. Never a query's text."
            ),
            input=Nothing,
            output=Activity,
            handler=activity,
            timeout_seconds=10,
            per_minute=10,
            per_day=500,
        ),
    ]
