"""The knowledge API and the indexer, through a real gRPC server and SQL database, with stand-ins
for Authorization, the embedding provider and Qdrant. SQLite has no row-level security or
triggers, so `make knowledge-check` proves those on PostgreSQL."""

import asyncio
import hashlib
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool
from windops.knowledge.v1 import knowledge_pb2 as pb
from windops.knowledge.v1 import knowledge_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_grpc import (
    ServiceRuntime,
    Settings,
    UnavailableError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
)
from windops_grpc.egress import EMBEDDINGS, Fixed, turned_off
from windops_knowledge import embeddings, library, vectors
from windops_knowledge.service import KnowledgeService
from windops_knowledge.store import INDEX_QUEUE, METADATA, SERVICE_NAME
from windops_outbox import OUTBOX_TABLE, create_outbox_table
from windops_simulator import documents

SECRET = "x" * 32
TENANT, OTHER = "north-sea", "atlas-wind"
SETTINGS = Settings(environment="test", version="test", identity_secret=SECRET, grpc_port=0,
                    http_port=0, shutdown_grace_seconds=1)  # fmt: skip
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
SET = {d.path: d for d in documents.load()}
GEARBOX = SET["oem/ws-prc-gbx-012-r3.md"]
SITE = SET["site/ns-site-off-001-r2.md"]
INJECTED = SET["evaluation/adv-sb-inj-001-r1.md"]


@dataclass
class FakeAuthorizer:
    """Grants as (user, permission, wind farm), the farm empty for the whole tenant."""

    grants: set[tuple[str, str, str]]
    asked: list[tuple[str, str]] = field(default_factory=list)
    down: bool = False

    async def allowed(self, permission: str, wind_farm_id: str = "") -> bool:
        if self.down:
            raise UnavailableError("Permissions cannot be checked right now")
        self.asked.append((permission, wind_farm_id))
        user = current_identity().user_id
        return (user, permission, "") in self.grants or (
            bool(wind_farm_id) and (user, permission, wind_farm_id) in self.grants
        )


DIMENSIONS = 64


def bag_of_words(text: str) -> list[float]:
    """A stand-in for a real embedding: texts sharing words point the same way."""
    vector = [0.0] * DIMENSIONS
    for word in re.findall(r"[a-z]{3,}", text.lower()):
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


@dataclass
class FakeEmbedder:
    provider: str = "openai"
    model: str = "text-embedding-3-large"
    version: str = embeddings.ADAPTER_VERSION
    dimensions: int = DIMENSIONS
    failure: Exception | None = None
    sent: list[str] = field(default_factory=list)

    async def embed(self, texts: Any) -> embeddings.Embedded:
        if self.failure is not None:
            raise self.failure
        self.sent.extend(texts)
        return embeddings.Embedded([bag_of_words(t) for t in texts], self.model, len(texts))


@dataclass
class FakeQdrant:
    points: dict[str, vectors.Point] = field(default_factory=dict)
    collections: dict[str, int] = field(default_factory=dict)
    searched: list[str] = field(default_factory=list)
    down: bool = False

    async def ensure_collection(self, name: str, dimensions: int) -> None:
        self.collections[name] = dimensions

    async def upsert(self, collection: str, points: list[vectors.Point]) -> None:
        self.points.update({p.point_id: p for p in points})

    async def search(
        self,
        collection: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        applies_to: str = "",
        types: tuple[str, ...] = (),
    ) -> list[vectors.Found]:
        self.searched.append(tenant_id)
        if self.down:
            raise vectors.VectorsUnavailableError("The vector store cannot be reached right now")
        found = [
            vectors.Found(p.document_id, p.revision, p.ordinal,
                          sum(a * b for a, b in zip(vector, p.vector, strict=True)),
                          {"tenant_id": p.tenant_id, **p.payload})
            for p in self.points.values()
            if p.tenant_id == tenant_id
            and (not applies_to or applies_to in p.payload["applies_to"])
            and (not types or p.payload["type"] in types)
        ]  # fmt: skip
        return sorted(found, key=lambda f: -f.score)[:limit]

    async def close(self) -> None:
        return None


@dataclass
class World:
    stub: Any
    engine: AsyncEngine
    embedder: FakeEmbedder
    qdrant: FakeQdrant
    clock: list[datetime]
    authorizer: FakeAuthorizer
    switches: Fixed

    async def index(self) -> int:
        """Run the indexer until nothing is due, and return how many it took."""
        taken = 0
        while await library.index_next(
            self.engine, self.embedder, self.qdrant, self.clock[0], self.switches
        ):
            taken += 1
        return taken


def as_user(user: str, level: str | None = LEVEL_MFA, tenant: str = TENANT) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=tenant, user_id=user, auth_level=level,
                                auth_time=int(time.time()) if level else None)  # fmt: skip
    return (("x-windops-identity", sign_identity(identity, SECRET)),)


def upload_request(document: documents.Document, key: str = "") -> pb.UploadDocumentRequest:
    return pb.UploadDocumentRequest(
        filename=document.path.rsplit("/", 1)[-1], content_type="text/markdown",
        content=document.text().encode(), source="synthetic-documents-1", idempotency_key=key,
    )  # fmt: skip


async def upload(world: World, document: documents.Document, user: str = "u-admin",
                 key: str = "", tenant: str = TENANT) -> pb.DocumentVersion:  # fmt: skip
    metadata = as_user(user, tenant=tenant)
    answer = await world.stub.UploadDocument(upload_request(document, key), metadata=metadata,
                                             timeout=5)  # fmt: skip
    kept: pb.DocumentVersion = answer.document
    return kept


async def code_of(call: Awaitable[Any]) -> grpc.StatusCode:
    try:
        await call
    except grpc.aio.AioRpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


async def get(world: World, document_id: str, revision: int = 0, user: str = "u-reader",
              tenant: str = TENANT) -> Any:  # fmt: skip
    return await world.stub.GetDocument(
        pb.GetDocumentRequest(document_id=document_id, revision=revision),
        metadata=as_user(user, tenant=tenant), timeout=5,
    )  # fmt: skip


def with_service(scenario: Callable[[World], Awaitable[None]]) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level

    async def run() -> None:
        engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
        async with engine_.begin() as connection:
            await connection.run_sync(METADATA.create_all)
            await connection.run_sync(create_outbox_table)
        grants = {(u, p, "") for u in ("u-admin", "u-engineer")
                  for p in ("knowledge.read", "knowledge.manage")}  # fmt: skip
        grants |= {("u-reader", "knowledge.read", ""),
                   ("u-dogger", "knowledge.read", "dogger-east"),
                   ("u-hornsea", "knowledge.read", "hornsea-west"),
                   ("u-walney", "knowledge.read", "walney"),
                   ("u-tenant-admin", "knowledge.manage", "")}  # fmt: skip
        clock = [NOW]
        authorizer, embedder, qdrant = FakeAuthorizer(grants), FakeEmbedder(), FakeQdrant()
        switches = Fixed()

        def register(server: grpc.aio.Server) -> None:
            knowledge_pb2_grpc.add_KnowledgeServiceServicer_to_server(
                KnowledgeService(engine_, authorizer, embedder, qdrant, clock=lambda: clock[0],
                                 egress=switches),
                server,
            )  # fmt: skip

        runtime = ServiceRuntime(SERVICE_NAME, SETTINGS, register, install_global_telemetry=False)
        await runtime.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as channel:
                stub = knowledge_pb2_grpc.KnowledgeServiceStub(channel)
                await scenario(World(stub, engine_, embedder, qdrant, clock, authorizer, switches))
        finally:
            await runtime.stop()
            await engine_.dispose()

    try:
        asyncio.run(run())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


async def audited(world: World) -> list[str]:
    from windops_events import parse_event

    o = OUTBOX_TABLE.c
    async with world.engine.connect() as connection:
        rows = (await connection.execute(select(o.subject, o.envelope).order_by(o.sequence))).all()
    return [parse_event(row.subject, row.envelope).payload.action  # type: ignore[attr-defined]
            for row in rows if "audit" in row.subject]  # fmt: skip


def test_an_upload_keeps_its_metadata_and_is_indexed_with_its_model_recorded() -> None:
    async def scenario(world: World) -> None:
        kept = await upload(world, GEARBOX)
        m = kept.metadata
        assert (m.document_id, m.revision, m.classification) == ("WS-PRC-GBX-012", 3, "internal")
        assert (m.supersedes_document_id, m.supersedes_revision) == ("WS-PRC-GBX-012", 2)
        assert kept.status == "pending" and kept.passages > 5 and kept.ingested_by == "u-admin"
        assert "document_id" in kept.metadata_from_document
        assert await world.index() == 1
        found = (await get(world, "WS-PRC-GBX-012")).document
        assert found.status == "indexed" and found.status_reason == ""
        assert (found.embedding_provider, found.embedding_model, found.embedding_version) == (
            "openai",
            "text-embedding-3-large",
            embeddings.ADAPTER_VERSION,
        )
        assert found.indexed_at.ToDatetime(tzinfo=UTC) == NOW
        # Each point carries its model and version, and its tenant, never its text (LLM-011).
        points = list(world.qdrant.points.values())
        assert len(points) == found.passages
        assert all(p.tenant_id == TENANT and p.payload["embedding_model"] ==
                   "text-embedding-3-large" and p.payload["embedding_version"] ==
                   embeddings.ADAPTER_VERSION and "text" not in p.payload
                   for p in points)  # fmt: skip
        assert world.qdrant.collections == {"knowledge_text_embedding_3_large_64": 64}
        # What was embedded is each passage under its document's title and section.
        assert world.embedder.sent[0].startswith(GEARBOX.title)
        assert await audited(world) == ["knowledge_document.ingested",
                                        "knowledge_document.indexed"]  # fmt: skip

    with_service(scenario)


def test_who_may_upload_and_read() -> None:
    async def scenario(world: World) -> None:
        request = upload_request(GEARBOX)
        for metadata in (as_user("u-reader"), as_user("u-admin", LEVEL_PASSWORD),
                         as_user("u-nobody")):  # fmt: skip
            refused = world.stub.UploadDocument(request, metadata=metadata, timeout=5)
            assert await code_of(refused) == grpc.StatusCode.PERMISSION_DENIED
        await upload(world, GEARBOX)
        assert (await get(world, "WS-PRC-GBX-012")).document.metadata.revision == 3
        assert await code_of(get(world, "WS-PRC-GBX-012", user="u-nobody")) == \
            grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        # Another tenant has no such document.
        assert await code_of(get(world, "WS-PRC-GBX-012", tenant=OTHER)) == \
            grpc.StatusCode.NOT_FOUND  # fmt: skip

    with_service(scenario)


def test_a_retry_returns_the_first_answer_and_a_changed_revision_is_refused() -> None:
    async def scenario(world: World) -> None:
        first = await upload(world, GEARBOX, key="k-1")
        again = await upload(world, GEARBOX, key="k-1")
        assert again == first
        # The same revision with the same content, without a key, is the same upload.
        assert (await upload(world, GEARBOX)).ingested_at == first.ingested_at
        changed = upload_request(GEARBOX)
        changed.content = GEARBOX.text().replace("2 hours", "3 hours").encode()
        refused = world.stub.UploadDocument(changed, metadata=as_user("u-admin"), timeout=5)
        assert await code_of(refused) == grpc.StatusCode.FAILED_PRECONDITION
        reused = upload_request(SITE, key="k-1")
        assert (
            await code_of(world.stub.UploadDocument(reused, metadata=as_user("u-admin"), timeout=5))
            == grpc.StatusCode.INVALID_ARGUMENT
        )
        # Both revisions are kept, and the newest is what a plain read gives.
        await upload(world, SET["oem/ws-prc-gbx-012-r2.md"])
        found = await get(world, "WS-PRC-GBX-012")
        assert found.document.metadata.revision == 3 and list(found.revisions) == [2, 3]
        assert (await get(world, "WS-PRC-GBX-012", 2)).document.metadata.revision == 2

    with_service(scenario)


def test_a_refused_upload_keeps_nothing_and_says_why() -> None:
    async def scenario(world: World) -> None:
        secret = upload_request(GEARBOX)
        secret.content = (GEARBOX.text() + "\npassword: hunter2!\n").encode()
        try:
            await world.stub.UploadDocument(secret, metadata=as_user("u-admin"), timeout=5)
            raise AssertionError("a credential must be refused")
        except grpc.aio.AioRpcError as error:
            assert error.code() == grpc.StatusCode.INVALID_ARGUMENT
            assert "a password" in (error.details() or "")
            assert "hunter2" not in (error.details() or "")
        pdf = pb.UploadDocumentRequest(filename="x.pdf", content_type="application/pdf",
                                       content=b"%PDF", source="s")  # fmt: skip
        sent = world.stub.UploadDocument(pdf, metadata=as_user("u-admin"), timeout=5)
        assert await code_of(sent) == grpc.StatusCode.INVALID_ARGUMENT
        reader = as_user("u-reader")
        listed = await world.stub.ListDocuments(pb.ListDocumentsRequest(), metadata=reader,
                                                timeout=5)  # fmt: skip
        assert list(listed.documents) == [] and await audited(world) == []

    with_service(scenario)


def test_a_document_addressed_to_an_ai_is_held_until_someone_else_releases_it() -> None:
    async def scenario(world: World) -> None:
        held = await upload(world, INJECTED)
        assert held.status == "held" and held.screening
        assert {f.kind for f in held.screening} == {"instructions_to_ai"}
        assert await world.index() == 0 and world.embedder.sent == []

        def release(user: str, key: str = "") -> Any:
            return world.stub.ReleaseDocument(pb.ReleaseDocumentRequest(
                document_id="ADV-SB-INJ-001", revision=1, reason="Reviewed: test material",
                idempotency_key=key), metadata=as_user(user), timeout=5)  # fmt: skip

        assert await code_of(release("u-admin")) == grpc.StatusCode.FAILED_PRECONDITION
        assert await code_of(release("u-reader")) == grpc.StatusCode.PERMISSION_DENIED
        released = (await release("u-engineer", "r-1")).document
        assert released.status == "pending" and released.released_by == "u-engineer"
        assert (await release("u-engineer", "r-1")).document == released
        assert await code_of(release("u-engineer")) == grpc.StatusCode.FAILED_PRECONDITION
        assert await world.index() == 1
        assert "knowledge_document.held" in await audited(world)
        assert "knowledge_document.released" in await audited(world)

    with_service(scenario)


def test_nothing_above_the_tenants_allowance_goes_to_the_provider() -> None:
    async def scenario(world: World) -> None:
        settings = await world.stub.GetIngestionSettings(
            pb.GetIngestionSettingsRequest(), metadata=as_user("u-reader"), timeout=5
        )
        assert settings.settings.embedding_max_classification == "confidential"
        managed = await world.stub.GetIngestionSettings(
            pb.GetIngestionSettingsRequest(), metadata=as_user("u-tenant-admin"), timeout=5
        )
        assert managed.settings == settings.settings, "read by one who manages without reading"
        nobody = world.stub.GetIngestionSettings(
            pb.GetIngestionSettingsRequest(), metadata=as_user("u-stranger"), timeout=5
        )
        assert await code_of(nobody) == grpc.StatusCode.PERMISSION_DENIED
        site = await upload(world, SITE)
        assert site.status == "not_indexed" and "restricted" in site.status_reason
        assert await world.index() == 0

        def allow(classification: str, user: str = "u-admin", **kw: Any) -> Any:
            return world.stub.SetIngestionSettings(pb.SetIngestionSettingsRequest(
                embedding_max_classification=classification, reason="Synthetic documents only"),
                metadata=as_user(user, **kw), timeout=5)  # fmt: skip

        assert await code_of(allow("restricted", "u-reader")) == grpc.StatusCode.PERMISSION_DENIED
        assert await code_of(allow("restricted", level=LEVEL_PASSWORD)) == \
            grpc.StatusCode.PERMISSION_DENIED  # fmt: skip
        assert await code_of(allow("secret")) == grpc.StatusCode.INVALID_ARGUMENT
        changed = (await allow("restricted")).settings
        assert changed.embedding_max_classification == "restricted"
        assert changed.updated_by == "u-admin"
        assert (await get(world, "NS-SITE-OFF-001")).document.status == "pending"
        # Lowered again before the indexer reached it: it is not sent.
        await allow("internal")
        assert await world.index() == 1 and world.embedder.sent == []
        assert (await get(world, "NS-SITE-OFF-001")).document.status == "not_indexed"
        assert "knowledge_settings.changed" in await audited(world)

    with_service(scenario)


def test_an_outage_pauses_indexing_and_a_refusal_ends_it() -> None:
    async def scenario(world: World) -> None:
        await upload(world, GEARBOX)
        world.embedder.failure = embeddings.EmbeddingUnavailableError(
            "The embedding provider cannot be reached right now"
        )
        assert await world.index() == 1
        waiting = (await get(world, "WS-PRC-GBX-012")).document
        assert waiting.status == "pending" and "cannot be reached" in waiting.status_reason
        # Not again until its backoff has passed, which grows with each attempt.
        assert await world.index() == 0
        async with world.engine.connect() as connection:
            due = (await connection.execute(select(INDEX_QUEUE))).one()
        assert due.attempts == 1
        assert due.next_attempt_at.replace(tzinfo=UTC) == NOW + timedelta(
            seconds=library.FIRST_RETRY_SECONDS
        )
        world.embedder.failure = None
        world.clock[0] = NOW + timedelta(minutes=1)
        assert await world.index() == 1
        assert (await get(world, "WS-PRC-GBX-012")).document.status == "indexed"
        # A refusal is final.
        await upload(world, SET["oem/ws-prc-brg-021-r1.md"])
        world.embedder.failure = embeddings.EmbeddingRefusedError(
            "The embedding provider refused the request: 400"
        )
        assert await world.index() == 1 and await world.index() == 0
        failed = (await get(world, "WS-PRC-BRG-021")).document
        assert failed.status == "failed" and "400" in failed.status_reason

    with_service(scenario)


def test_embeddings_turned_off_hold_indexing_and_pause_search_until_on_again() -> None:
    """EGR-002: nothing goes to the embedding provider, and the documents and searches say why."""

    async def scenario(world: World) -> None:
        world.switches.off.add(EMBEDDINGS.name)
        await upload(world, GEARBOX)
        assert await world.index() == 1
        waiting = (await get(world, "WS-PRC-GBX-012")).document
        assert waiting.status == "pending"
        assert waiting.status_reason.endswith(turned_off(EMBEDDINGS))
        assert world.embedder.sent == []
        search = world.stub.SearchPassages(
            pb.SearchPassagesRequest(query="gearbox oil temperature"),
            metadata=as_user("u-reader"), timeout=5,
        )  # fmt: skip
        try:
            await search
            raise AssertionError("A search embeds the question, so it must pause")
        except grpc.aio.AioRpcError as error:
            assert error.code() is grpc.StatusCode.UNAVAILABLE
            assert turned_off(EMBEDDINGS) in (error.details() or "")
        world.switches.off.clear()
        world.clock[0] = NOW + timedelta(minutes=1)
        assert await world.index() == 1
        assert (await get(world, "WS-PRC-GBX-012")).document.status == "indexed"

    with_service(scenario)


def test_the_list_gives_each_documents_newest_revision_a_page_at_a_time() -> None:
    async def scenario(world: World) -> None:
        demo = [d for d in SET.values() if d.purpose == "demo" and TENANT in d.tenants]
        for document in demo:
            await upload(world, document)
        newest = sorted({d.document_id for d in demo})
        seen, token = [], ""
        while True:
            page = await world.stub.ListDocuments(
                pb.ListDocumentsRequest(page_size=4, page_token=token),
                metadata=as_user("u-reader"), timeout=5)  # fmt: skip
            seen += [(d.metadata.document_id, d.metadata.revision) for d in page.documents]
            token = page.next_page_token
            if not token:
                break
        assert [d for d, _ in seen] == newest
        assert ("WS-PRC-GBX-012", 3) in seen
        held = await world.stub.ListDocuments(pb.ListDocumentsRequest(status="not_indexed"),
                                              metadata=as_user("u-reader"), timeout=5)  # fmt: skip
        assert [d.metadata.document_id for d in held.documents] == ["NS-SITE-OFF-001"]

    with_service(scenario)


def test_a_library_is_browsed_and_read_only_where_the_reader_may_read() -> None:
    async def scenario(world: World) -> None:
        for document in (GEARBOX, SITE):
            await upload(world, document)

        def listed(user: str, farm: str = "") -> Any:
            return world.stub.ListDocuments(pb.ListDocumentsRequest(wind_farm_id=farm),
                                            metadata=as_user(user), timeout=5)  # fmt: skip

        def passages(user: str, document_id: str, farm: str = "") -> Any:
            return world.stub.ListPassages(
                pb.ListPassagesRequest(document_id=document_id, wind_farm_id=farm),
                metadata=as_user(user), timeout=5)  # fmt: skip

        both = ["NS-SITE-OFF-001", "WS-PRC-GBX-012"]
        assert [d.metadata.document_id for d in (await listed("u-reader")).documents] == both
        # Scoped to one wind farm, a reader browses there, and only there.
        assert await code_of(listed("u-dogger")) == grpc.StatusCode.PERMISSION_DENIED
        assert await code_of(listed("u-dogger", "walney")) == grpc.StatusCode.PERMISSION_DENIED
        assert await code_of(listed("u-dogger", "Not A Farm")) == grpc.StatusCode.INVALID_ARGUMENT
        dogger = await listed("u-dogger", "dogger-east")
        assert [d.metadata.document_id for d in dogger.documents] == both
        # The site document applies to other wind farms, so this reader never learns it exists.
        walney = await listed("u-walney", "walney")
        assert [d.metadata.document_id for d in walney.documents] == ["WS-PRC-GBX-012"]
        hidden = world.stub.GetDocument(
            pb.GetDocumentRequest(document_id="NS-SITE-OFF-001", wind_farm_id="walney"),
            metadata=as_user("u-walney"), timeout=5)  # fmt: skip
        assert await code_of(hidden) == grpc.StatusCode.NOT_FOUND
        assert await code_of(passages("u-walney", "NS-SITE-OFF-001", "walney")) == \
            grpc.StatusCode.NOT_FOUND  # fmt: skip
        assert (await passages("u-hornsea", "NS-SITE-OFF-001", "hornsea-west")).passages

    with_service(scenario)


def test_a_documents_passages_read_in_order_with_their_citations() -> None:
    async def scenario(world: World) -> None:
        await upload(world, SET["oem/ws-prc-gbx-012-r2.md"])
        await upload(world, GEARBOX)
        await world.index()
        read = await world.stub.ListPassages(pb.ListPassagesRequest(document_id="WS-PRC-GBX-012"),
                                             metadata=as_user("u-reader"), timeout=5)  # fmt: skip
        assert read.document.metadata.revision == 3
        assert read.document.passages == len(read.passages) > 1
        ordinals = [p.citation_id for p in read.passages]
        assert ordinals == [f"kn:WS-PRC-GBX-012@3#{n}" for n in range(len(ordinals))]
        assert all(p.current and p.score == 0 for p in read.passages)
        assert [p.first_line for p in read.passages] == sorted(p.first_line for p in read.passages)
        # Each is the same passage a citation reads.
        cited = await world.stub.GetPassage(
            pb.GetPassageRequest(citation_id=read.passages[0].citation_id),
            metadata=as_user("u-reader"), timeout=5)  # fmt: skip
        assert cited.passage.text == read.passages[0].text
        older = await world.stub.ListPassages(
            pb.ListPassagesRequest(document_id="WS-PRC-GBX-012", revision=2),
            metadata=as_user("u-reader"), timeout=5)  # fmt: skip
        assert {(p.current, p.superseded_by) for p in older.passages} == {
            (False, "WS-PRC-GBX-012@3")}  # fmt: skip
        missing = world.stub.ListPassages(pb.ListPassagesRequest(document_id="WS-NONE-001"),
                                          metadata=as_user("u-reader"), timeout=5)  # fmt: skip
        assert await code_of(missing) == grpc.StatusCode.NOT_FOUND
        nobody = world.stub.ListPassages(pb.ListPassagesRequest(document_id="WS-PRC-GBX-012"),
                                         metadata=as_user("u-stranger"), timeout=5)  # fmt: skip
        assert await code_of(nobody) == grpc.StatusCode.PERMISSION_DENIED

    with_service(scenario)
