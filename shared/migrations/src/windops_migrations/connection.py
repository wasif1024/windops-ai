"""A service's database connection, with a password locally or Entra on Azure (ADR 0006).

| `WINDOPS_DATABASE_AUTH` | How a service signs in to PostgreSQL |
| --- | --- |
| `password` | Its own login and password, from its Kubernetes Secret, verified against our CA |
| `entra` | Azure Database for PostgreSQL with Microsoft Entra: no stored password |

`auto`, the default, is `entra` when `WINDOPS_ENVIRONMENT` is `dev` or `live`, and `password`
otherwise. With Entra, each new connection presents a token from `DefaultAzureCredential`, which
uses Microsoft Entra Workload ID on AKS. Tokens last about an hour, so one is kept until shortly
before it ends, and the pool's connections are recycled before then. The server's certificate is
public, so it is verified against the system's public roots, never skipped.
"""

import os
import threading
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy import URL, Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from windops_migrations.runner import database_url

PASSWORD, ENTRA = "password", "entra"
AUTO = "auto"
ON_AZURE = frozenset({"dev", "live"})
# The scope Azure Database for PostgreSQL accepts tokens for.
POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
# A token is renewed this long before it ends, and pooled connections live no longer than this.
RENEW_SECONDS = 5 * 60
RECYCLE_SECONDS = 45 * 60


def database_auth(environ: Mapping[str, str] | None = None) -> str:
    """How the settings say to sign in to PostgreSQL."""
    env = os.environ if environ is None else environ
    chosen = env.get("WINDOPS_DATABASE_AUTH", AUTO).strip().lower() or AUTO
    if chosen == AUTO:
        return ENTRA if env.get("WINDOPS_ENVIRONMENT", "local").strip() in ON_AZURE else PASSWORD
    if chosen not in (PASSWORD, ENTRA):
        raise ValueError(f"WINDOPS_DATABASE_AUTH is one of: {AUTO}, {PASSWORD}, {ENTRA}")
    return chosen


class EntraTokens:
    """PostgreSQL access tokens, kept until shortly before each ends. Safe across threads."""

    def __init__(self, credential: Any = None, clock: Any = time.time) -> None:
        if credential is None:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential()
        self._credential = credential
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._expires = 0.0

    def current(self) -> str:
        with self._lock:
            if not self._token or self._clock() >= self._expires - RENEW_SECONDS:
                token = self._credential.get_token(POSTGRES_SCOPE)
                self._token, self._expires = token.token, float(token.expires_on)
            return self._token


def _url(auth: str) -> URL:
    if auth == PASSWORD:
        return database_url()
    values = {
        key: os.environ.get(f"WINDOPS_DATABASE_{key.upper()}", "")
        for key in ("host", "port", "name", "username")
    }
    missing = [f"WINDOPS_DATABASE_{key.upper()}" for key, value in values.items() if not value]
    if missing:
        raise ValueError(f"Missing database settings: {', '.join(missing)}")
    return URL.create(
        "postgresql+psycopg",
        host=values["host"],
        port=int(values["port"]),
        database=values["name"],
        # The Entra principal's name, as the database knows it: no password, a token instead.
        username=values["username"],
        query={"sslmode": "verify-full", "sslrootcert": "system"},
    )


def _sign_in_with_tokens(engine: Engine, tokens: EntraTokens) -> None:
    @event.listens_for(engine, "do_connect")
    def with_token(dialect: Any, record: Any, cargs: Any, cparams: dict[str, Any]) -> None:
        cparams["password"] = tokens.current()


def create_service_engine(
    *, tokens: EntraTokens | None = None, auth: str | None = None, **options: Any
) -> AsyncEngine:
    """The service's async engine, signed in as the settings say."""
    chosen = auth or database_auth()
    if chosen == PASSWORD:
        return create_async_engine(_url(PASSWORD), **options)
    options.setdefault("pool_recycle", RECYCLE_SECONDS)
    engine = create_async_engine(_url(ENTRA), **options)
    _sign_in_with_tokens(engine.sync_engine, tokens or EntraTokens())
    return engine


def create_sync_service_engine(
    *, tokens: EntraTokens | None = None, auth: str | None = None, **options: Any
) -> Engine:
    """The same, for synchronous work such as migrations."""
    chosen = auth or database_auth()
    if chosen == PASSWORD:
        return create_engine(_url(PASSWORD), **options)
    engine = create_engine(_url(ENTRA), **options)
    _sign_in_with_tokens(engine, tokens or EntraTokens())
    return engine
