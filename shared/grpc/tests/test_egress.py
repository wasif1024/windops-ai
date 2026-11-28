"""Egress switches: on until turned off, kept briefly, and closed when unknown (EGR-002)."""

import asyncio
from collections.abc import Mapping

from windops_grpc import UnavailableError
from windops_grpc.egress import (
    BY_NAME,
    CLAUDE,
    FORECASTS,
    PATHS,
    UNKNOWN,
    EgressSwitches,
    turned_off,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Tenants:
    """The Tenant Service's answers, or its outage."""

    def __init__(self, answer: Mapping[str, bool]) -> None:
        self.answer = dict(answer)
        self.down = False
        self.reads = 0

    async def read(self, tenant: str) -> Mapping[str, bool]:
        self.reads += 1
        if self.down:
            raise UnavailableError("The Tenant Service cannot be reached")
        return dict(self.answer)


def switches(answer: Mapping[str, bool]) -> tuple[EgressSwitches, Tenants, Clock]:
    tenants, clock = Tenants(answer), Clock()
    return EgressSwitches(tenants.read, ttl_seconds=30, clock=clock), tenants, clock


def test_every_path_says_where_it_goes_and_what_stops() -> None:
    assert [p.name for p in PATHS] == [
        "claude", "embeddings", "langsmith_traces", "langsmith_sandbox", "forecasts",
    ]  # fmt: skip
    assert BY_NAME["forecasts"] is FORECASTS
    for path in PATHS:
        assert ":" in path.destination and path.when_off.endswith(".")


def test_a_path_is_on_until_turned_off() -> None:
    gate, _, _ = switches({"claude": True})
    assert asyncio.run(gate.check("t1", FORECASTS)).allowed


def test_a_path_turned_off_says_so() -> None:
    gate, _, _ = switches({"claude": False})
    decision = asyncio.run(gate.check("t1", CLAUDE))
    assert not decision.allowed and decision.known
    assert (
        decision.reason
        == turned_off(CLAUDE)
        == ("Anthropic API is turned off for this tenant (EGR-002)")
    )


def test_an_answer_is_kept_briefly_then_read_again() -> None:
    gate, tenants, clock = switches({"claude": True})
    asyncio.run(gate.check("t1", CLAUDE))
    tenants.answer["claude"] = False
    clock.now = 29
    assert asyncio.run(gate.check("t1", CLAUDE)).allowed
    assert tenants.reads == 1
    clock.now = 30
    assert not asyncio.run(gate.check("t1", CLAUDE)).allowed
    assert tenants.reads == 2


def test_each_tenant_is_read_on_its_own() -> None:
    gate, tenants, _ = switches({"claude": True})
    asyncio.run(gate.check("t1", CLAUDE))
    asyncio.run(gate.check("t2", CLAUDE))
    assert tenants.reads == 2


def test_with_the_tenant_service_down_the_last_answer_stands() -> None:
    gate, tenants, clock = switches({"claude": False})
    asyncio.run(gate.check("t1", CLAUDE))
    tenants.down = True
    clock.now = 600
    assert asyncio.run(gate.check("t1", CLAUDE)).reason == turned_off(CLAUDE)
    assert asyncio.run(gate.check("t1", FORECASTS)).allowed


def test_with_no_answer_ever_nothing_leaves() -> None:
    gate, tenants, _ = switches({})
    tenants.down = True
    decision = asyncio.run(gate.check("t1", FORECASTS))
    assert (decision.allowed, decision.reason, decision.known) == (False, UNKNOWN, False)
    tenants.down = False
    assert asyncio.run(gate.check("t1", FORECASTS)).allowed
