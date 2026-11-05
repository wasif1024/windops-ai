"""Settings and revisions. `make migrate-check` runs the real migrations on the cluster."""

import sys
from pathlib import Path

import pytest
from windops_migrations import (
    MissingSettingError,
    create_revision,
    database_url,
    find_violations,
    migrations_directory,
)
from windops_migrations.__main__ import main

SETTINGS = {
    "WINDOPS_DATABASE_HOST": "db.example",
    "WINDOPS_DATABASE_PORT": "5432",
    "WINDOPS_DATABASE_NAME": "windops_audit",
    "WINDOPS_DATABASE_USERNAME": "windops_audit",
    "WINDOPS_DATABASE_PASSWORD": "p@ss/word:1",
    "WINDOPS_TLS_DIR": "/var/run/windops/tls",
}
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def service_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A package with the same migrations directory the service template creates."""
    source = ROOT / "scripts" / "service_database_template" / "src" / "migrations"
    target = tmp_path / "probe_package" / "migrations"
    (target / "versions").mkdir(parents=True)
    (tmp_path / "probe_package" / "__init__.py").touch()
    for name in ("env.py", "script.py.mako"):
        (target / name).write_text((source / f"{name}.tmpl").read_text())
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("probe_package", None)
    return "probe_package"


def test_url_comes_from_the_settings(settings: None) -> None:
    url = database_url()
    assert (url.host, url.port, url.database) == ("db.example", 5432, "windops_audit")
    assert url.drivername == "postgresql+psycopg"


def test_a_password_with_special_characters_stays_intact(settings: None) -> None:
    assert database_url().password == SETTINGS["WINDOPS_DATABASE_PASSWORD"]


def test_the_password_is_hidden_when_the_url_is_shown(settings: None) -> None:
    assert SETTINGS["WINDOPS_DATABASE_PASSWORD"] not in str(database_url())


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_a_missing_setting_is_named(
    settings: None, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    with pytest.raises(MissingSettingError, match=name):
        database_url()


def test_an_error_about_settings_never_shows_values(
    settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WINDOPS_DATABASE_HOST")
    with pytest.raises(MissingSettingError) as error:
        database_url()
    assert SETTINGS["WINDOPS_DATABASE_PASSWORD"] not in str(error.value)


def test_migrations_directory_is_inside_the_package(service_package: str) -> None:
    assert migrations_directory(service_package).name == "migrations"


def test_a_new_revision_needs_no_database_and_starts_as_expand(service_package: str) -> None:
    path = create_revision(service_package, "add items")
    assert path.name.startswith("rev_")
    assert path.name.endswith("_add_items.py")
    assert 'phase: str = "expand"' in path.read_text()
    assert find_violations(path) == []


def test_revisions_form_one_line(service_package: str) -> None:
    first = create_revision(service_package, "first")
    second = create_revision(service_package, "second")
    revision = re_revision(first)
    assert f"down_revision: str | None = '{revision}'" in second.read_text()


def re_revision(path: Path) -> str:
    return path.name.split("_")[1]


def test_command_line_adds_a_revision(
    service_package: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", service_package, "--message", "from cli"]) == 0
    assert "_from_cli.py" in capsys.readouterr().out


def test_command_line_needs_a_known_command() -> None:
    with pytest.raises(SystemExit):
        main(["destroy", "windops_audit"])


@pytest.mark.usefixtures("settings")
def test_the_address_checks_the_servers_certificate_and_name() -> None:
    """Weaker modes would accept any certificate, or none (GRP-005)."""
    query = database_url().query
    assert query["sslmode"] == "verify-full"
    assert query["sslrootcert"] == "/var/run/windops/tls/ca.crt"


@pytest.mark.usefixtures("settings")
def test_the_address_needs_the_certificate_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINDOPS_TLS_DIR")
    with pytest.raises(MissingSettingError, match="WINDOPS_TLS_DIR"):
        database_url()
