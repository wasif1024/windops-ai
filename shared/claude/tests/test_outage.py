"""Claude down: its breaker opens, refuses without calling, and runs can tell (SRS 7.7)."""

import asyncio
from decimal import Decimal
from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from langchain_core.messages import HumanMessage
from test_claude_policy import POLICY, claude, reply
from windops_claude import (
    Budget,
    BudgetExhaustedError,
    Claude,
    ClaudeUnreachableError,
    MemoryLedger,
    Meter,
    claude_breaker,
    is_claude_outage,
)
from windops_claude.outage import DEPENDENCY, breaker_config
from windops_resilience import BreakerOpenError, BreakerState, BulkheadFullError

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def status(kind: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return kind("failed", response=httpx.Response(code, request=REQUEST), body=None)


class Unreachable:
    """A model whose calls never get an answer."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages: list[Any]) -> Any:
        self.calls += 1
        raise self.error


def guarded(error: Exception) -> tuple[Claude, Unreachable, Any]:
    model = Unreachable(error)
    breaker = claude_breaker()
    budget = Budget(tokens=100_000, cost_usd=Decimal(10))
    client = Claude(
        POLICY, Meter(budget, MemoryLedger()), lambda: "key",
        make_model=lambda *_, **__: model, breaker=breaker,  # type: ignore[arg-type]
    )  # fmt: skip
    return client, model, breaker


def ask(client: Claude) -> None:
    asyncio.run(client.invoke("supervisor", [HumanMessage("x")]))


@pytest.mark.parametrize(
    "error",
    [
        BreakerOpenError(DEPENDENCY),
        BulkheadFullError(DEPENDENCY),
        ClaudeUnreachableError("down"),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.APITimeoutError(REQUEST),
        status(anthropic.RateLimitError, 429),
        status(anthropic.InternalServerError, 529),
        status(anthropic.InternalServerError, 500),
    ],
)
def test_claude_down_is_an_outage(error: Exception) -> None:
    assert is_claude_outage(error)


@pytest.mark.parametrize(
    "error",
    [
        BreakerOpenError("knowledge"),
        status(anthropic.BadRequestError, 400),
        status(anthropic.AuthenticationError, 401),
        BudgetExhaustedError("spent"),
        ValueError("anything else"),
    ],
)
def test_a_runs_own_error_is_not_an_outage(error: Exception) -> None:
    assert not is_claude_outage(error)


def test_an_outage_is_found_through_the_errors_wrapped_around_it() -> None:
    try:
        try:
            raise ClaudeUnreachableError("down")
        except ClaudeUnreachableError as error:
            raise RuntimeError("the step failed") from error
    except RuntimeError as wrapped:
        assert is_claude_outage(wrapped)


def test_the_breaker_suits_a_run_of_few_long_calls() -> None:
    config = breaker_config()
    assert (config.minimum_calls, config.window_size) == (3, 10)
    assert config.slow_call_seconds < 300, "below the 5-minute request limit"


def test_no_connection_counts_against_the_breaker_until_it_refuses_without_calling() -> None:
    client, model, breaker = guarded(anthropic.APIConnectionError(request=REQUEST))
    for _ in range(3):
        with pytest.raises(ClaudeUnreachableError):
            ask(client)
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(BreakerOpenError):
        ask(client)
    assert model.calls == 3


def test_anthropic_overloaded_counts_against_the_breaker() -> None:
    client, _, breaker = guarded(status(anthropic.InternalServerError, 529))
    for _ in range(3):
        with pytest.raises(anthropic.InternalServerError):
            ask(client)
    assert breaker.state is BreakerState.OPEN


def test_a_bad_request_never_opens_the_breaker() -> None:
    client, _, breaker = guarded(status(anthropic.BadRequestError, 400))
    for _ in range(5):
        with pytest.raises(anthropic.BadRequestError):
            ask(client)
    assert breaker.state is BreakerState.CLOSED


def test_an_answer_through_the_breaker_is_as_without_it() -> None:
    ledger = MemoryLedger()
    plain, _ = claude(reply(), Budget(tokens=1_000, cost_usd=Decimal(1)), ledger)
    breaker = claude_breaker()
    guarded_client = Claude(
        POLICY, Meter(Budget(tokens=1_000, cost_usd=Decimal(1)), ledger), lambda: "key",
        make_model=plain._make, breaker=breaker,
    )  # fmt: skip
    result = asyncio.run(guarded_client.invoke("supervisor", [HumanMessage("x")]))
    assert result.call.cost_usd > 0 and breaker.bulkhead.in_flight == 0
