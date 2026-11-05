"""Check a token's level of authentication, on claims that have already been verified.

The `acr` claim alone is not enough for step-up. Keycloak keeps reporting `mfa` on refreshed tokens
after the level's time limit has passed, as `make mfa-check` shows. So a recent step-up also needs a
recent `auth_time`.
"""

import time
from collections.abc import Callable, Mapping

LEVEL_PASSWORD = "password"  # noqa: S105  # A level's name, not a password.
LEVEL_MFA = "mfa"
# How long a step-up lasts (SEC-003). Keycloak's login flow uses the same limit.
MFA_LEVEL_SECONDS = 300
# Allowed difference between this machine's clock and Keycloak's.
_CLOCK_SKEW_SECONDS = 5


class MfaRequiredError(Exception):
    """The user has not used a second factor, so administrative access is refused (SEC-002)."""


class StepUpRequiredError(MfaRequiredError):
    """The user must use a second factor again, because the last time is too old (SEC-003)."""


def require_mfa(claims: Mapping[str, object]) -> None:
    """Refuse a token that was not issued with a second factor (SEC-002)."""
    if claims.get("acr") != LEVEL_MFA:
        raise MfaRequiredError("This operation needs multi-factor authentication")


def require_recent_mfa(
    claims: Mapping[str, object],
    *,
    max_age_seconds: int = MFA_LEVEL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> None:
    """Refuse a token unless a second factor was used within the time limit (SEC-003)."""
    require_mfa(claims)
    authenticated_at = claims.get("auth_time")
    # bool is an int in Python, and must not pass as a time.
    if isinstance(authenticated_at, bool) or not isinstance(authenticated_at, int | float):
        raise StepUpRequiredError("The token does not say when the user authenticated")
    age = clock() - authenticated_at
    if age > max_age_seconds or age < -_CLOCK_SKEW_SECONDS:
        raise StepUpRequiredError("This operation needs a recent second factor")
