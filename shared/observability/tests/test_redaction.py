import pytest
from windops_observability import REDACTED, redact, redact_text


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer abc.def-123",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "sk-ant-api03-abcdefghijklmnop",
        "sk-proj-abcdefghijklmnopqrst",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "github_pat_abcdefghijklmnopqrstuvwxyz",
        "lsv2_pt_abcdefghijklmnopqrst",
        "tvly-abcdefghijklmnopqrstuvwx",
        "AKIAABCDEFGHIJKLMNOP",
        "operator@example.com",
        "password=hunter2",
        "api_key: abc123",
        "token=abc123",
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_sensitive_text_is_removed(secret: str) -> None:
    result = redact_text(f"before {secret} after")
    assert secret not in result
    assert REDACTED in result
    assert result.startswith("before ")


def test_url_password_is_removed_and_host_is_kept() -> None:
    result = redact_text("postgres://windops:hunter2@db:5432/telemetry")
    assert "hunter2" not in result
    assert "db:5432/telemetry" in result


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "client_secret",
        "access_token",
        "refresh_token",
        "api_key",
        "x-api-key",
        "Authorization",
        "cookie",
        "set-cookie",
        "private_key",
        "credentials",
        "session_id",
        "email",
    ],
)
def test_sensitive_keys_are_removed(key: str) -> None:
    assert redact({key: "plain-value"}) == {key: REDACTED}


@pytest.mark.parametrize(
    "key",
    [
        "input_tokens",
        "output_tokens",
        "token_usage",
        "tenant_id",
        "turbine_id",
        "idempotency_key",
        "content_hash",
        "secret_name",
        "fortunecookie",
    ],
)
def test_operational_keys_are_kept(key: str) -> None:
    assert redact({key: "plain-value"}) == {key: "plain-value"}


def test_nested_values_are_redacted() -> None:
    value = {"outer": {"password": "x", "items": [{"token": "y"}, "user@example.com", 7]}}
    assert redact(value) == {
        "outer": {"password": REDACTED, "items": [{"token": REDACTED}, REDACTED, 7]}
    }


def test_ordinary_text_and_numbers_are_unchanged() -> None:
    assert redact_text("Gearbox temperature reached 92 C") == "Gearbox temperature reached 92 C"
    assert redact(92.5) == 92.5


def test_input_is_not_changed() -> None:
    value = {"password": "x"}
    redact(value)
    assert value == {"password": "x"}
