"""Each call to Claude is counted for the LLM Operations dashboard, with fixed labels only."""

from decimal import Decimal
from typing import Any

import anthropic
import httpx2 as httpx
import pytest
from windops_claude import telemetry
from windops_claude.budget import BudgetExhaustedError, Call
from windops_claude.pricing import Usage


class Recorder:
    def __init__(self) -> None:
        self.seen: list[tuple[float, dict[str, Any]]] = []

    def add(self, value: float, attributes: dict[str, Any]) -> None:
        self.seen.append((value, attributes))

    record = add


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    found = {name: Recorder() for name in ("_calls", "_tokens", "_cost", "_duration", "_errors")}
    for name, recorder in found.items():
        monkeypatch.setattr(telemetry, name, recorder)
    return found


def call(**changes: Any) -> Call:
    values: dict[str, Any] = {
        "role": "supervisor", "requested_model": "claude-opus-5-5",
        "served_model": "claude-opus-5-5", "effort": "high", "stop_reason": "end_turn",
        "fallback": False,
        "usage": Usage(input_tokens=120, cache_write_5m_tokens=0, cache_write_1h_tokens=900,
                       cache_read_tokens=4000, output_tokens=300),
        "cost_usd": Decimal("0.0421"),
    }  # fmt: skip
    return Call(**{**values, **changes})


def test_an_answer_counts_its_tokens_by_kind_its_cost_and_its_stop_reason(
    recorded: dict[str, Recorder],
) -> None:
    telemetry.answered(call())
    labels = {"role": "supervisor", "model": "claude-opus-5-5"}
    assert recorded["_calls"].seen == [
        (1, {**labels, "stop_reason": "end_turn", "fallback": False})
    ]
    assert {a["kind"]: v for v, a in recorded["_tokens"].seen} == {
        "input": 120, "cache_read": 4000, "cache_write": 900, "output": 300,
    }  # fmt: skip
    ((cost, cost_labels),) = recorded["_cost"].seen
    assert cost == pytest.approx(0.0421)
    assert cost_labels == labels


def test_a_refusal_is_counted_as_an_answer_with_its_stop_reason(
    recorded: dict[str, Recorder],
) -> None:
    telemetry.answered(call(stop_reason="refusal", fallback=True, cost_usd=Decimal(0),
                            usage=Usage(input_tokens=10)))  # fmt: skip
    ((_, labels),) = recorded["_calls"].seen
    assert (labels["stop_reason"], labels["fallback"]) == ("refusal", True)
    assert recorded["_cost"].seen == [], "nothing to add"


def _status(kind: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return kind("failed", response=httpx.Response(code, request=request), body=None)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_status(anthropic.RateLimitError, 429), "rate_limited"),
        (_status(anthropic.InternalServerError, 529), "overloaded"),
        (_status(anthropic.InternalServerError, 500), "server_error"),
        (_status(anthropic.BadRequestError, 400), "rejected"),
        (anthropic.APITimeoutError(httpx.Request("POST", "https://api.anthropic.com")), "timeout"),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")),
         "unreachable"),
        (BudgetExhaustedError("spent"), "budget_exhausted"),
        (ValueError("anything else"), "other"),
    ],
)  # fmt: skip
def test_a_call_with_no_answer_is_counted_by_what_failed(
    recorded: dict[str, Recorder], error: BaseException, kind: str
) -> None:
    with pytest.raises(type(error)), telemetry.timed("scada-analyst", "claude-opus-5-5"):
        raise error
    assert recorded["_errors"].seen == [
        (1, {"role": "scada-analyst", "model": "claude-opus-5-5", "error": kind})
    ]
    ((seconds, labels),) = recorded["_duration"].seen
    assert seconds >= 0 and labels["outcome"] == kind


def test_a_call_answered_is_timed_as_answered(recorded: dict[str, Recorder]) -> None:
    with telemetry.timed("supervisor", "claude-opus-5-5"):
        pass
    assert recorded["_errors"].seen == []
    assert recorded["_duration"].seen[0][1]["outcome"] == "answered"
