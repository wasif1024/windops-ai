import jwt
import pytest
from windops_grpc import IdentityError, VerifiedIdentity, sign_identity, verify_identity

SECRET = "x" * 64
IDENTITY = VerifiedIdentity(subject="svc", tenant_id="tenant-a", user_id="user-1")


def test_round_trip() -> None:
    assert verify_identity(sign_identity(IDENTITY, SECRET), SECRET) == IDENTITY


def test_user_is_optional_for_service_principals() -> None:
    identity = VerifiedIdentity(subject="fault-investigator", tenant_id="tenant-a")
    assert verify_identity(sign_identity(identity, SECRET), SECRET).user_id is None


@pytest.mark.parametrize("token", [None, "", "not-a-token"])
def test_missing_or_malformed_token_is_rejected(token: str | None) -> None:
    with pytest.raises(IdentityError):
        verify_identity(token, SECRET)


def test_wrong_key_is_rejected() -> None:
    token = sign_identity(IDENTITY, "another-secret-that-is-long-enough-to-sign")
    with pytest.raises(IdentityError):
        verify_identity(token, SECRET)


def test_tampered_token_is_rejected() -> None:
    header, payload, signature = sign_identity(IDENTITY, SECRET).split(".")
    other = sign_identity(VerifiedIdentity("svc", "tenant-b"), SECRET).split(".")[1]
    with pytest.raises(IdentityError):
        verify_identity(f"{header}.{other}.{signature}", SECRET)
    assert payload != other


def test_expired_token_is_rejected() -> None:
    token = sign_identity(IDENTITY, SECRET, ttl_seconds=60, clock=lambda: 1000.0)
    assert verify_identity(token, SECRET, clock=lambda: 1060.0) == IDENTITY
    with pytest.raises(IdentityError):
        verify_identity(token, SECRET, clock=lambda: 1065.0)


def test_unsigned_token_is_rejected() -> None:
    claims = {"sub": "svc", "tenant_id": "a", "aud": "windops-internal", "exp": 9999999999}
    token = jwt.encode(claims, key="", algorithm="none")
    with pytest.raises(IdentityError):
        verify_identity(token, SECRET)


@pytest.mark.parametrize("missing", ["sub", "tenant_id", "aud", "exp"])
def test_required_claims_are_enforced(missing: str) -> None:
    claims: dict[str, object] = {
        "sub": "svc",
        "tenant_id": "a",
        "aud": "windops-internal",
        "exp": 9999999999,
    }
    del claims[missing]
    token = jwt.encode(claims, SECRET, algorithm="HS256")
    with pytest.raises(IdentityError):
        verify_identity(token, SECRET)


def test_a_different_signing_algorithm_is_rejected() -> None:
    claims = {"sub": "svc", "tenant_id": "a", "aud": "windops-internal", "exp": 9999999999}
    with pytest.raises(IdentityError):
        verify_identity(jwt.encode(claims, SECRET, algorithm="HS512"), SECRET)


def test_wrong_audience_is_rejected() -> None:
    claims = {"sub": "svc", "tenant_id": "a", "aud": "other", "exp": 9999999999}
    with pytest.raises(IdentityError):
        verify_identity(jwt.encode(claims, SECRET, algorithm="HS256"), SECRET)
