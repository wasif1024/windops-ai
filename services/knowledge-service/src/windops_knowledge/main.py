"""Start the Knowledge Service: its API, the indexer, and the relay for its audit records."""

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import httpx
from opentelemetry import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.authorization.v1 import authorization_pb2_grpc
from windops.knowledge.v1 import knowledge_pb2_grpc
from windops.tenant.v1 import tenant_pb2_grpc
from windops_auth import current_identity_token
from windops_events import EventsUnavailableError, open_events
from windops_grpc import ServiceClient, create_channel, run_service
from windops_grpc.egress import Gate, egress_switches
from windops_migrations import create_service_engine
from windops_observability import resilience_metrics
from windops_outbox import OutboxRelay
from windops_resilience import BreakerConfig, CircuitBreaker

from windops_knowledge import embeddings, library, vectors
from windops_knowledge.service import AuthorizationClient, KnowledgeService
from windops_knowledge.store import SERVICE_NAME

logger = logging.getLogger("windops.service")
RECONNECT_SECONDS = 5
# How often the indexer looks for work when it has none.
IDLE_SECONDS = 2.0
DEFAULT_QDRANT_URL = "https://windops-qdrant.windops-data.svc.cluster.local:6333"
SECRETS = Path("/var/run/windops/secrets")


async def relay_events(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Publish queued audit records. An outage only delays them (SRS 7.7)."""
    events = None
    while events is None:
        if stop.is_set():
            return
        try:
            events = await open_events(SERVICE_NAME)
        except EventsUnavailableError as error:
            logger.warning(
                "Cannot reach the events back end yet", extra={"error_type": type(error).__name__}
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_SECONDS)
    try:
        await OutboxRelay(engine, events.publisher).run_forever(stop)
    finally:
        await events.close()


async def run_indexer(
    engine: AsyncEngine,
    embedder: embeddings.Embedder,
    store: vectors.VectorStore,
    stop: asyncio.Event,
    egress: Gate | None = None,
) -> None:
    """Index whatever is due, and wait when nothing is. An error waits, and tries again."""
    try:
        while not stop.is_set():
            try:
                worked = await library.index_next(
                    engine, embedder, store, datetime.now(UTC), egress
                )
            except Exception as error:  # The indexer must outlive any one failure.
                logger.warning("Indexing failed", extra={"error_type": type(error).__name__})
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_SECONDS)
    finally:
        await store.close()


class _Lazy:
    """Makes its client on first use, because a channel must be made on the running loop."""

    def __init__(self, make: Any) -> None:
        self._make, self._made = make, None

    def __getattr__(self, name: str) -> Any:
        if self._made is None:
            self._made = self._make()
        return getattr(self._made, name)


def _breaker(dependency: str, slow_call_seconds: float = 1.0) -> CircuitBreaker:
    return CircuitBreaker(
        dependency,
        BreakerConfig(slow_call_seconds=slow_call_seconds),
        **resilience_metrics(metrics.get_meter_provider()),
    )


def main() -> None:
    engine = create_service_engine(pool_size=5, max_overflow=5, pool_pre_ping=True)
    tls = Path(os.environ["WINDOPS_TLS_DIR"])
    authorizer = _Lazy(lambda: AuthorizationClient(
        authorization_pb2_grpc.AuthorizationServiceStub(
            create_channel(os.environ["WINDOPS_AUTHORIZATION_ADDRESS"], tls)),
        ServiceClient(_breaker("authorization"), identity_token=current_identity_token),
    ))  # fmt: skip

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    # One of each, shared by the API's searches and the indexer. The provider's TLS is verified
    # against the system's authorities, and Qdrant's against the cluster's, always (ADR 0011).
    embedder = _Lazy(lambda: embeddings.OpenAIEmbedder(
        os.environ.get("WINDOPS_EMBEDDING_URL", embeddings.OPENAI_URL),
        SECRETS / "openai" / "api-key",
        _breaker("openai-embeddings", slow_call_seconds=10.0), httpx.AsyncClient(),
    ))  # fmt: skip
    store = _Lazy(lambda: vectors.Qdrant(
        os.environ.get("WINDOPS_QDRANT_URL", DEFAULT_QDRANT_URL),
        SECRETS / "qdrant" / "api-key", tls / "ca.crt", _breaker("qdrant", 5.0),
    ))  # fmt: skip

    # Each tenant may turn embeddings off (EGR-002, ADR 0048).
    egress = egress_switches(
        _Lazy(lambda: tenant_pb2_grpc.TenantServiceStub(
            create_channel(os.environ["WINDOPS_TENANT_ADDRESS"], tls))),
        ServiceClient(_breaker("tenant"), identity_token=current_identity_token),
        SERVICE_NAME, os.environ["WINDOPS_IDENTITY_SECRET"],
    )  # fmt: skip

    def register(server: grpc.aio.Server) -> None:
        knowledge_pb2_grpc.add_KnowledgeServiceServicer_to_server(
            KnowledgeService(engine, authorizer, embedder, store, egress=egress), server
        )

    async def relay(stop: asyncio.Event) -> None:
        await relay_events(engine, stop)

    async def indexer(stop: asyncio.Event) -> None:
        await run_indexer(engine, embedder, store, stop, egress)

    # Ready once the database answers: an outage of the provider or Qdrant pauses indexing,
    # and must not take the API away with it (SRS 7.7).
    asyncio.run(run_service(SERVICE_NAME, register, readiness_checks=(database_ready,),
                            background=(relay, indexer)))  # fmt: skip


if __name__ == "__main__":
    main()
