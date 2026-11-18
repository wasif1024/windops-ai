"""Limits shared by every replica, failing closed, and audit records naming the actor (MCP-003)."""

from types import TracebackType
from typing import Any, Self, cast

import pytest
from mcp_support import TENANT, USER, run_async
from pydantic import Field
from redis.exceptions import ConnectionError as RedisConnectionError
from windops_cache import cache_breaker
from windops_mcp import (
    AuditUnavailableError,
    EventsAudit,
    LimitReachedError,
    LimitsUnavailableError,
    MemoryLimits,
    RedisLimits,
    Tool,
    ToolCall,
    ToolCaller,
    ToolInput,
    ToolOutput,
    audit_event,
)


class Query(ToolInput):
    query: str = Field(max_length=10)


class Done(ToolOutput):
    ok: bool


async def handle(_: Any) -> Done:
    return Done(ok=True)


TOOL = Tool(
    server="weather", name="get_forecast", title="Forecast", description="A forecast.",
    input=Query, output=Done, handler=handle, per_minute=2, per_day=3,
)  # fmt: skip
CALLER = ToolCaller(user_id=USER, tenant_id=TENANT, scopes=frozenset(), expires_at=0)


class Pipeline:
    def __init__(self, owner: "Redis") -> None:
        self.owner, self.key = owner, ""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def incr(self, key: str) -> None:
        self.key = key

    def expire(self, key: str, ttl: int) -> None:
        self.owner.ttls[key] = ttl

    async def execute(self) -> list[int]:
        if self.owner.down:
            raise RedisConnectionError("down")
        self.owner.counts[self.key] = self.owner.counts.get(self.key, 0) + 1
        return [self.owner.counts[self.key], 1]


class Redis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.down = False

    def pipeline(self, transaction: bool) -> Pipeline:
        return Pipeline(self)


def redis_limits(fake: Redis) -> RedisLimits:
    return RedisLimits(cast(Any, fake), service="mcp-weather", breaker=cache_breaker())


@run_async
async def test_redis_counts_each_caller_per_minute_under_the_servers_tenant_keys() -> None:
    fake = Redis()
    limits = redis_limits(fake)
    await limits.admit(CALLER, TOOL)
    await limits.admit(CALLER, TOOL)
    with pytest.raises(LimitReachedError) as raised:
        await limits.admit(CALLER, TOOL)
    assert raised.value.which == "per_minute"
    assert all(
        key.startswith(f"mcp-weather:{TENANT}:rate:tool:get_forecast:") for key in fake.counts
    )
    # The user's ID is never put in a key as it is.
    assert not any(USER in key for key in fake.counts)
    assert sorted(set(fake.ttls.values())) == [60, 86400]


@run_async
async def test_the_tenants_daily_quota_covers_every_caller() -> None:
    limits = redis_limits(Redis())
    others = [ToolCaller(f"user-{n}", TENANT, frozenset(), 0) for n in range(4)]
    for caller in others[:3]:
        await limits.admit(caller, TOOL)
    with pytest.raises(LimitReachedError) as raised:
        await limits.admit(others[3], TOOL)
    assert raised.value.which == "per_day"


@run_async
async def test_when_redis_is_down_nothing_is_let_through() -> None:
    fake = Redis()
    fake.down = True
    with pytest.raises(LimitsUnavailableError):
        await redis_limits(fake).admit(CALLER, TOOL)


@run_async
async def test_memory_limits_count_the_same_way() -> None:
    clock = [0.0]
    limits = MemoryLimits(clock=lambda: clock[0])
    await limits.admit(CALLER, TOOL)
    await limits.admit(CALLER, TOOL)
    with pytest.raises(LimitReachedError):
        await limits.admit(CALLER, TOOL)
    clock[0] += 61
    await limits.admit(CALLER, TOOL)
    clock[0] += 61
    with pytest.raises(LimitReachedError) as raised:
        await limits.admit(CALLER, TOOL)
    assert raised.value.which == "per_day"


def test_a_users_call_is_audited_as_the_user() -> None:
    event = audit_event(ToolCall("weather", "get_forecast", CALLER, "succeeded", "req-1"))
    assert (event.actor_type, event.actor_id, event.on_behalf_of) == ("user", USER, "")
    assert (event.action, event.resource_type, event.resource_id) == (
        "mcp.weather.get_forecast", "mcp_tool", "weather/get_forecast",
    )  # fmt: skip
    assert (event.outcome, event.correlation_id) == ("succeeded", "req-1")


def test_an_agents_call_names_the_agent_and_the_user_behind_it() -> None:
    """SEC-010."""
    agent = ToolCaller(USER, TENANT, frozenset(), 0, agent="fault-investigator")
    event = audit_event(ToolCall("weather", "get_forecast", agent, "denied", "req-2"))
    assert (event.actor_type, event.actor_id, event.on_behalf_of) == (
        "agent", "fault-investigator", USER,
    )  # fmt: skip


def test_an_unknown_outcome_cannot_be_recorded() -> None:
    with pytest.raises(ValueError):
        ToolCall("weather", "get_forecast", CALLER, "maybe", "req-3")


class Publisher:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, Any]] = []
        self.fail = fail

    async def publish(self, subject: str, *, tenant_id: str, payload: Any, **_: Any) -> Any:
        if self.fail:
            raise ConnectionError("down")
        self.sent.append((subject, tenant_id, payload))


@run_async
async def test_records_go_to_the_servers_own_audit_subject_in_the_callers_tenant() -> None:
    publisher = Publisher()
    audit = EventsAudit(cast(Any, publisher), producer="mcp-weather")
    await audit.record(ToolCall("weather", "get_forecast", CALLER, "succeeded", "req-1"))
    [(subject, tenant, payload)] = publisher.sent
    assert (subject, tenant) == ("audit.requested.mcp-weather", TENANT)
    assert payload.action == "mcp.weather.get_forecast"
    with pytest.raises(AuditUnavailableError):
        await EventsAudit(cast(Any, Publisher(fail=True)), producer="mcp-weather").record(
            ToolCall("weather", "get_forecast", CALLER, "succeeded", "req-1")
        )
