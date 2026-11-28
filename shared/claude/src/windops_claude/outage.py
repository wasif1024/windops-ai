"""Claude down: what counts, and the breaker that guards it (SRS 7.7, RES-001 to RES-003).

The Anthropic SDK retries a call itself, twice, so each call through the breaker is at most three
attempts, and nothing retries above it (RES-003). A run that finds Claude down pauses, resumable,
instead of spending its attempts (SRS 7.7).
"""

from collections.abc import Callable

import anthropic
from windops_resilience import BreakerConfig, BreakerOpenError, CircuitBreaker

# The breaker's name, which its metrics and alerts carry as the dependency.
DEPENDENCY = "anthropic"


class ClaudeUnreachableError(ConnectionError):
    """No answer came: the connection failed or timed out, after the SDK's own retries.

    A ConnectionError, so the breaker counts it as Claude failing.
    """


def breaker_config() -> BreakerConfig:
    """A run makes few calls, and a long answer can take minutes.

    So it opens on 3 failures in its last 10 calls, not SRS 7.7's starting 10 in 20, and only a
    call past 4 minutes, near the 5-minute request limit, counts as slow.
    """
    return BreakerConfig(slow_call_seconds=240.0, window_size=10, minimum_calls=3)


def claude_breaker(
    *,
    on_state_change: Callable[[str, str, str], None] | None = None,
    on_bulkhead_full: Callable[[str], None] | None = None,
) -> CircuitBreaker:
    """One breaker for every call a runtime replica makes to Claude, with its metrics hooks."""
    return CircuitBreaker(
        DEPENDENCY,
        breaker_config(),
        on_state_change=on_state_change,
        on_bulkhead_full=on_bulkhead_full,
    )


def _causes(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def is_claude_outage(error: BaseException) -> bool:
    """Whether a run failed because Claude is down, not because of its own request or answer.

    True for Claude's breaker refusing the call, a call that got no answer, and Anthropic's own
    429, 529 and 5xx. A refusal, a bad request or an exhausted budget is the run's own, and False.
    It looks through the errors a step wraps around the one it met.
    """
    for cause in _causes(error):
        if isinstance(cause, BreakerOpenError):
            return cause.dependency == DEPENDENCY
        if isinstance(cause, ClaudeUnreachableError | anthropic.APIConnectionError):
            return True
        if isinstance(cause, anthropic.APIStatusError):
            return cause.status_code == 429 or cause.status_code >= 500
    return False
