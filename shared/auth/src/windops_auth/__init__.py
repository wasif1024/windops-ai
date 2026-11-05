"""Signed internal identity, and checks on how a user authenticated (SEC-002, SEC-003)."""

from windops_auth.identity import (
    IDENTITY_METADATA_KEY,
    IdentityError,
    VerifiedIdentity,
    current_identity,
    current_identity_token,
    current_peer_service,
    identity_scope,
    sign_identity,
    verify_identity,
)
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
    "IDENTITY_METADATA_KEY",
    "LEVEL_MFA",
    "LEVEL_PASSWORD",
    "MFA_LEVEL_SECONDS",
    "IdentityError",
    "MfaRequiredError",
    "StepUpRequiredError",
    "VerifiedIdentity",
    "current_identity",
    "current_identity_token",
    "current_peer_service",
    "identity_scope",
    "require_mfa",
    "require_recent_mfa",
    "sign_identity",
    "verify_identity",
]
