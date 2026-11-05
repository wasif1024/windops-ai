"""A token must prove a second factor, and a recent one for sensitive operations."""

import pytest
from windops_auth import (
    LEVEL_MFA,
    LEVEL_PASSWORD,
    MFA_LEVEL_SECONDS,
    MfaRequiredError,
    StepUpRequiredError,
    require_mfa,
    require_recent_mfa,
)

NOW = 1_000_000.0


def clock() -> float:
    return NOW


def test_mfa_passes_with_the_mfa_level() -> None:
    require_mfa({"acr": LEVEL_MFA})


@pytest.mark.parametrize(
    "claims",
    [{"acr": LEVEL_PASSWORD}, {"acr": "0"}, {"acr": ""}, {"acr": None}, {"acr": 2}, {}],
    ids=["password", "zero", "empty", "null", "number", "missing"],
)
def test_anything_else_is_refused(claims: dict[str, object]) -> None:
    """Deny by default: only the exact level passes (SEC-002)."""
    with pytest.raises(MfaRequiredError):
        require_mfa(claims)


def test_a_recent_second_factor_passes() -> None:
    require_recent_mfa({"acr": LEVEL_MFA, "auth_time": NOW - 10}, clock=clock)


def test_a_second_factor_at_the_limit_passes() -> None:
    require_recent_mfa({"acr": LEVEL_MFA, "auth_time": NOW - MFA_LEVEL_SECONDS}, clock=clock)


def test_a_refreshed_token_past_the_limit_is_refused() -> None:
    """Keycloak still reports mfa here, as `make mfa-check` shows (SEC-003)."""
    claims = {"acr": LEVEL_MFA, "auth_time": NOW - MFA_LEVEL_SECONDS - 1}
    with pytest.raises(StepUpRequiredError):
        require_recent_mfa(claims, clock=clock)


def test_a_recent_password_alone_is_refused() -> None:
    with pytest.raises(MfaRequiredError):
        require_recent_mfa({"acr": LEVEL_PASSWORD, "auth_time": NOW}, clock=clock)


@pytest.mark.parametrize(
    "value",
    [None, "1000000", True],
    ids=["null", "text", "boolean"],
)
def test_an_unusable_time_is_refused(value: object) -> None:
    with pytest.raises(StepUpRequiredError):
        require_recent_mfa({"acr": LEVEL_MFA, "auth_time": value}, clock=clock)


def test_a_missing_time_is_refused() -> None:
    with pytest.raises(StepUpRequiredError):
        require_recent_mfa({"acr": LEVEL_MFA}, clock=clock)


def test_a_time_in_the_future_is_refused() -> None:
    with pytest.raises(StepUpRequiredError):
        require_recent_mfa({"acr": LEVEL_MFA, "auth_time": NOW + 60}, clock=clock)


def test_a_small_clock_difference_is_allowed() -> None:
    require_recent_mfa({"acr": LEVEL_MFA, "auth_time": NOW + 3}, clock=clock)


def test_a_shorter_limit_can_be_set() -> None:
    with pytest.raises(StepUpRequiredError):
        require_recent_mfa(
            {"acr": LEVEL_MFA, "auth_time": NOW - 60}, max_age_seconds=30, clock=clock
        )


def test_step_up_failures_are_also_mfa_failures() -> None:
    """A caller that handles the wider error still refuses both."""
    assert issubclass(StepUpRequiredError, MfaRequiredError)
