"""Decide whether a verified token shows enough authentication (SEC-002, SEC-003)."""

from windops_auth.levels import (
    LEVEL_MFA,
    LEVEL_PASSWORD,
    MFA_LEVEL_SECONDS,
    MfaRequiredError,
    StepUpRequiredError,
    require_mfa,
    require_recent_mfa,
)

__all__ = [
    "LEVEL_MFA",
    "LEVEL_PASSWORD",
    "MFA_LEVEL_SECONDS",
    "MfaRequiredError",
    "StepUpRequiredError",
    "require_mfa",
    "require_recent_mfa",
]
