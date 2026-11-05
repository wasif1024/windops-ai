"""Signed internal identity, which lives in windops-auth (M3.3)."""

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

__all__ = [
    "IDENTITY_METADATA_KEY",
    "IdentityError",
    "VerifiedIdentity",
    "current_identity",
    "current_identity_token",
    "current_peer_service",
    "identity_scope",
    "sign_identity",
    "verify_identity",
]
