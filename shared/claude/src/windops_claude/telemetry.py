"""What every call to Claude did, as metrics for the LLM Operations dashboard (SRS 10.3).

Each call is counted with its role, the model that served it, its stop reason and whether it
fell back, and its tokens, cost and latency are added up. A call that fails before Claude
answers is counted by what failed: rate limited, overloaded, timed out, unreachable, refused
by the API, or out of budget. Labels are fixed values only, never content. Each carries its
provider: "anthropic", or "stand-in" for a dry run's stand-in API, which the dashboards leave out.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import anthropic
from opentelemetry import metrics
from windops_resilience import BreakerOpenError

from windops_claude.budget import BudgetExhaustedError, Call
from windops_claude.outage import ClaudeUnreachableError

_meter = metrics.get_meter("windops.claude")
_calls = _meter.create_counter(
    "windops_llm_calls", description="Calls Claude answered, by role, model and stop reason"
)
_tokens = _meter.create_counter(
    "windops_llm_tokens", unit="{token}",
    description="Tokens, by kind: input, cache_read, cache_write and output",
)  # fmt: skip
_cost = _meter.create_counter(
    "windops_llm_cost_usd", description="Estimated cost of Claude's answers, in US dollars"
)
_duration = _meter.create_histogram(
    "windops.llm.call.duration", unit="s",
    description="Time to Claude's whole answer, by role, model and outcome",
    explicit_bucket_boundaries_advisory=[0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300],
)  # fmt: skip
_errors = _meter.create_counter(
    "windops_llm_errors", description="Calls that got no answer, by what failed"
)


def error_kind(error: BaseException) -> str:
    """A fixed name for what failed, for a metric's label."""
    if isinstance(error, BudgetExhaustedError):
        return "budget_exhausted"
    if isinstance(error, BreakerOpenError):
        return "breaker_open"
    if isinstance(error, ClaudeUnreachableError):
        error = error.__cause__ or error
    if isinstance(error, anthropic.RateLimitError):
        return "rate_limited"
    if isinstance(error, anthropic.InternalServerError):
        # 529 overloaded and every other 5xx.
        return "overloaded" if error.status_code == 529 else "server_error"
    if isinstance(error, anthropic.APITimeoutError):
        return "timeout"
    if isinstance(error, anthropic.APIConnectionError):
        return "unreachable"
    if isinstance(error, anthropic.APIStatusError):
        return "rejected"
    return "other"


def answered(call: Call, provider: str = "anthropic") -> None:
    """Count a call Claude answered, whatever its stop reason: every answer is paid for."""
    labels = {"role": call.role, "model": call.served_model, "provider": provider}
    _calls.add(1, {**labels, "stop_reason": call.stop_reason or "unknown",
                   "fallback": call.fallback})  # fmt: skip
    usage = call.usage
    for kind, count in (
        ("input", usage.input_tokens), ("cache_read", usage.cache_read_tokens),
        ("cache_write", usage.cache_write_5m_tokens + usage.cache_write_1h_tokens),
        ("output", usage.output_tokens),
    ):  # fmt: skip
        if count:
            _tokens.add(count, {**labels, "kind": kind})
    if call.cost_usd > Decimal(0):
        _cost.add(float(call.cost_usd), labels)


@contextmanager
def timed(role: str, model: str, provider: str = "anthropic") -> Iterator[None]:
    """Time one call to Claude, and count it as an error if no answer came."""
    started = time.perf_counter()
    outcome = "answered"
    try:
        yield
    except BaseException as error:
        outcome = error_kind(error)
        _errors.add(1, {"role": role, "model": model, "provider": provider, "error": outcome})
        raise
    finally:
        _duration.record(time.perf_counter() - started,
                         {"role": role, "model": model, "provider": provider,
                          "outcome": outcome})  # fmt: skip
