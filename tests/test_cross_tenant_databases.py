"""The cross-tenant check attacks every service that stores data, so a new one cannot be missed."""

from pathlib import Path

from scripts.checks.identity.cross_tenant import DATABASES

REPOSITORY = Path(__file__).resolve().parents[1]


def test_every_service_with_migrations_is_attacked() -> None:
    stored = {
        folder.parents[3].name.removesuffix("-service").replace("-", "_")
        for folder in REPOSITORY.glob("services/*/src/*/migrations/versions")
        if any(folder.glob("rev_*.py"))
    }
    assert set(DATABASES) == stored
