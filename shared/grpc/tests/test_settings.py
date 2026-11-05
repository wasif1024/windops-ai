import pytest
from windops_grpc import Settings
from windops_grpc.settings import SettingsError

SECRET = "x" * 32
TLS = {"WINDOPS_TLS_DIR": "/var/run/windops/tls"}


def test_defaults() -> None:
    settings = Settings.from_env({"WINDOPS_IDENTITY_SECRET": SECRET, **TLS})
    assert settings.grpc_port == 50051
    assert settings.http_port == 8080
    assert settings.environment == "local"
    assert settings.otlp_endpoint is None


def test_values_come_from_the_environment() -> None:
    settings = Settings.from_env(
        {
            **TLS,
            "WINDOPS_IDENTITY_SECRET": SECRET,
            "WINDOPS_ENVIRONMENT": "ci",
            "WINDOPS_VERSION": "1.2.3",
            "WINDOPS_GRPC_PORT": "6000",
            "WINDOPS_LOG_LEVEL": "debug",
            "WINDOPS_OTLP_ENDPOINT": "http://collector:4318",
        }
    )
    assert settings.environment == "ci"
    assert settings.version == "1.2.3"
    assert settings.grpc_port == 6000
    assert settings.log_level == "DEBUG"
    assert settings.otlp_endpoint == "http://collector:4318"


@pytest.mark.parametrize("env", [{}, {"WINDOPS_IDENTITY_SECRET": "short"}])
def test_missing_or_short_secret_stops_startup(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        Settings.from_env(env)


@pytest.mark.parametrize("extra", [{"WINDOPS_GRPC_PORT": "abc"}, {"WINDOPS_LOG_LEVEL": "LOUD"}])
def test_invalid_values_stop_startup(extra: dict[str, str]) -> None:
    with pytest.raises(SettingsError):
        Settings.from_env({"WINDOPS_IDENTITY_SECRET": SECRET, **TLS, **extra})


def test_secret_is_hidden_from_repr() -> None:
    assert SECRET not in repr(Settings.from_env({"WINDOPS_IDENTITY_SECRET": SECRET, **TLS}))
