"""Apply one service's migrations to its own database."""

import importlib.resources
import logging
import os
from pathlib import Path

from alembic import command, context
from alembic.config import Config
from sqlalchemy import URL, create_engine, select, text
from sqlalchemy import func as sql_func
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# Any fixed number works, because each service has its own database.
MIGRATION_LOCK_ID = 7_260_109
# A table lock held by running queries must not leave every query behind this one waiting.
LOCK_TIMEOUT = "10s"
_SETTINGS = {
    "host": "WINDOPS_DATABASE_HOST",
    "port": "WINDOPS_DATABASE_PORT",
    "database": "WINDOPS_DATABASE_NAME",
    "username": "WINDOPS_DATABASE_USERNAME",
    "password": "WINDOPS_DATABASE_PASSWORD",
}


class MissingSettingError(RuntimeError):
    """A database setting is missing, so the Job's Secret is wrong."""


def database_url() -> URL:
    """Return the service's database address, from its Secret (DEP-003)."""
    values = {key: os.environ.get(name, "") for key, name in _SETTINGS.items()}
    missing = [_SETTINGS[key] for key, value in values.items() if not value]
    if missing:
        raise MissingSettingError(f"Missing database settings: {', '.join(missing)}")
    certificates = os.environ.get("WINDOPS_TLS_DIR", "")
    if not certificates:
        # Without this, a driver could settle for TLS it has not checked (GRP-005).
        raise MissingSettingError("Missing database settings: WINDOPS_TLS_DIR")
    return URL.create(
        "postgresql+psycopg",
        host=values["host"],
        port=int(values["port"]),
        database=values["database"],
        username=values["username"],
        password=values["password"],
        query={"sslmode": "verify-full", "sslrootcert": f"{certificates}/ca.crt"},
    )


def run_migrations() -> None:
    """Alembic's entry point, called from each service's migrations/env.py."""
    engine = create_engine(database_url(), poolclass=NullPool)
    with engine.connect() as connection:
        # Held until this connection closes. Another Job waits here, then finds nothing to do.
        connection.execute(select(sql_func.pg_advisory_lock(MIGRATION_LOCK_ID)))
        # Set after the wait above, which is meant to last as long as the other Job takes.
        connection.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        connection.commit()
        # One transaction per revision, so a failure undoes only that revision, and completely.
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


def migrations_directory(package: str) -> Path:
    """Return a service package's migrations directory."""
    return Path(str(importlib.resources.files(package) / "migrations"))


def _config(package: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(migrations_directory(package)))
    # A name that is also a valid Python module, which a bare revision ID may not be.
    config.set_main_option("file_template", "rev_%%(rev)s_%%(slug)s")
    return config


def upgrade(package: str) -> None:
    """Apply every revision a service has not yet run."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    command.upgrade(_config(package), "head")


def create_revision(package: str, message: str) -> Path:
    """Add an empty revision to a service's history. Needs no database."""
    script = command.revision(_config(package), message=message)
    if isinstance(script, list) or script is None:
        raise RuntimeError("Alembic did not create one revision")
    return Path(script.path)
