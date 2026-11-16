"""Ingestion and indexing, shared by the API and the demo seed, so both take the same path.

A revision is kept as it is uploaded, with its passages, in one transaction, and audited. Then:

- with screening findings it is **held**, until a person other than its uploader releases it;
- above the tenant's allowed classification it is **not_indexed**: kept, never sent out;
- otherwise it is **pending**, and queued.

The indexer takes the queue one revision at a time, embeds its passages, writes them to Qdrant,
and marks it **indexed**. When the provider or Qdrant cannot be reached, the revision stays pending
and is tried again later, backing off; ingestion pauses, nothing is lost (SRS 7.7). A refusal by
the provider is final: the revision is **failed**, with the reason.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops_grpc import ConflictError, InvalidRequestError, NotFoundError
from windops_migrations import in_tenant

from windops_knowledge import embeddings, passages, uploads, vectors
from windops_knowledge.store import DOCUMENTS, INDEX_QUEUE, PASSAGES, SETTINGS, audit

logger = logging.getLogger("windops.service")
PENDING, INDEXED, HELD, NOT_INDEXED, FAILED = (
    "pending", "indexed", "held", "not_indexed", "failed",
)  # fmt: skip
# Until a Tenant Administrator changes it: restricted documents stay inside the platform.
DEFAULT_ALLOWANCE = "confidential"
SERVICE = "knowledge"
FIRST_RETRY_SECONDS, LONGEST_RETRY_SECONDS = 30, 1800


def rank(classification: str) -> int:
    return uploads.CLASSIFICATIONS.index(classification)


@dataclass(frozen=True)
class Allowance:
    embedding_max_classification: str
    updated_at: datetime | None
    updated_by: str


async def allowance(connection: AsyncConnection, tenant: str) -> Allowance:
    row = (
        await connection.execute(select(SETTINGS).where(SETTINGS.c.tenant_id == tenant))
    ).one_or_none()
    if row is None:
        return Allowance(DEFAULT_ALLOWANCE, None, "")
    return Allowance(row.embedding_max_classification, row.updated_at, row.updated_by)


def _not_indexed_reason(classification: str, allowed: str) -> str:
    return (
        f"A {classification} document is above what this tenant sends to the embedding provider, "
        f"{allowed} at most. It is kept, but not searchable"
    )


async def _enqueue(connection: AsyncConnection, tenant: str, document_id: str, revision: int,
                   now: datetime) -> None:  # fmt: skip
    await connection.execute(
        insert(INDEX_QUEUE).values(
            tenant_id=tenant,
            document_id=document_id,
            revision=revision,
            attempts=0,
            next_attempt_at=now,
        )
    )


async def ingest(
    connection: AsyncConnection, tenant: str, actor_type: str, actor: str,
    upload: uploads.Upload, *, filename: str, content_type: str, source: str, now: datetime,
) -> tuple[Any, bool]:  # fmt: skip
    """Keep one revision, in the caller's transaction. Return its row, and whether it is new.

    The same revision uploaded again with the same content is the same upload, and changes
    nothing. With different content it is refused: a revision, once published, does not change.
    """
    m, d = upload.metadata, DOCUMENTS.c
    found = (
        await connection.execute(
            select(DOCUMENTS).where(
                d.tenant_id == tenant, d.document_id == m.document_id, d.revision == m.revision
            )
        )
    ).one_or_none()
    if found is not None:
        if found.content_hash == upload.content_hash:
            return found, False
        raise ConflictError(
            f"{m.document_id} revision {m.revision} is already kept, with other content. A "
            "changed document is a new revision"
        )
    allowed = (await allowance(connection, tenant)).embedding_max_classification
    if upload.screening:
        status, reason = (
            HELD,
            ("It holds text addressed to an AI, so it waits for a person to review and release it"),
        )
    elif rank(m.classification) > rank(allowed):
        status, reason = NOT_INDEXED, _not_indexed_reason(m.classification, allowed)
    else:
        status, reason = PENDING, "Waiting to be indexed"
    split = passages.split(upload.text)
    if not split:
        raise InvalidRequestError("The document has no text to index")
    row = (
        await connection.execute(
            insert(DOCUMENTS).values(
                tenant_id=tenant, document_id=m.document_id, revision=m.revision, title=m.title,
                type=m.type, published=m.published, issued_by=m.issued_by,
                applies_to=json.dumps(list(m.applies_to)), classification=m.classification,
                supersedes_document_id=m.supersedes_document_id,
                supersedes_revision=m.supersedes_revision,
                metadata_from_document=json.dumps(list(upload.from_document)), source=source,
                filename=filename, content_type=content_type, content=upload.text,
                size_bytes=upload.size_bytes, content_hash=upload.content_hash, ingested_at=now,
                ingested_by=actor,
                screening=json.dumps([f.__dict__ for f in upload.screening]),
                status=status, status_reason=reason, released_by="", embedding_provider="",
                embedding_model="", embedding_version="", indexed_at=None,
            ).returning(*DOCUMENTS.c)
        )
    ).one()  # fmt: skip
    await connection.execute(insert(PASSAGES), [
        {"tenant_id": tenant, "document_id": m.document_id, "revision": m.revision,
         "ordinal": p.ordinal, "section": p.section, "text": p.text,
         "start_offset": p.start_offset, "end_offset": p.end_offset,
         "first_line": p.first_line, "last_line": p.last_line, "content_hash": p.content_hash}
        for p in split
    ])  # fmt: skip
    if status == PENDING:
        await _enqueue(connection, tenant, m.document_id, m.revision, now)
    resource = f"{m.document_id}@{m.revision}"
    await audit(connection, tenant_id=tenant, actor_type=actor_type, actor_id=actor,
                action="knowledge_document.ingested", resource_id=resource)  # fmt: skip
    if status == HELD:
        await audit(connection, tenant_id=tenant, actor_type=SERVICE, actor_id=SERVICE,
                    action="knowledge_document.held", resource_id=resource)  # fmt: skip
    return row, True


async def document(connection: AsyncConnection, tenant: str, document_id: str,
                   revision: int) -> Any:  # fmt: skip
    d = DOCUMENTS.c
    query = select(DOCUMENTS).where(d.tenant_id == tenant, d.document_id == document_id)
    if revision:
        query = query.where(d.revision == revision)
    row = (await connection.execute(query.order_by(d.revision.desc()).limit(1))).one_or_none()
    if row is None:
        raise NotFoundError("No such document")
    return row


async def release(connection: AsyncConnection, tenant: str, user: str, document_id: str,
                  revision: int, now: datetime) -> Any:  # fmt: skip
    """A held revision goes on to be indexed, as its classification allows.

    Never by the person who uploaded it."""
    row = await document(connection, tenant, document_id, revision)
    if row.status != HELD:
        raise ConflictError(f"{document_id} revision {row.revision} is not held, but {row.status}")
    if row.ingested_by == user:
        raise ConflictError("A held document is released by someone other than its uploader")
    allowed = (await allowance(connection, tenant)).embedding_max_classification
    if rank(row.classification) > rank(allowed):
        status, reason = NOT_INDEXED, _not_indexed_reason(row.classification, allowed)
    else:
        status, reason = PENDING, "Released, and waiting to be indexed"
    d = DOCUMENTS.c
    updated = (
        await connection.execute(
            update(DOCUMENTS)
            .where(d.tenant_id == tenant, d.document_id == document_id,
                   d.revision == row.revision, d.status == HELD)
            .values(status=status, status_reason=reason, released_by=user)
            .returning(*DOCUMENTS.c)
        )
    ).one()  # fmt: skip
    if status == PENDING:
        await _enqueue(connection, tenant, document_id, row.revision, now)
    await audit(connection, tenant_id=tenant, actor_type="user", actor_id=user,
                action="knowledge_document.released",
                resource_id=f"{document_id}@{row.revision}")  # fmt: skip
    return updated


async def set_allowance(connection: AsyncConnection, tenant: str, actor_type: str, actor: str,
                        classification: str, now: datetime) -> Allowance:  # fmt: skip
    """Change what may be sent out. Revisions it now allows are queued; nothing sent is recalled."""
    if classification not in uploads.CLASSIFICATIONS:
        raise InvalidRequestError(f"The classification is one of "
                                  f"{', '.join(uploads.CLASSIFICATIONS)}")  # fmt: skip
    s = SETTINGS.c
    exists = (await connection.execute(select(s.tenant_id).where(s.tenant_id == tenant))).first()
    values = {"embedding_max_classification": classification, "updated_at": now,
              "updated_by": actor}  # fmt: skip
    if exists:
        await connection.execute(update(SETTINGS).where(s.tenant_id == tenant).values(**values))
    else:
        await connection.execute(insert(SETTINGS).values(tenant_id=tenant, **values))
    d = DOCUMENTS.c
    allowed = [c for c in uploads.CLASSIFICATIONS if rank(c) <= rank(classification)]
    now_allowed = (
        await connection.execute(
            update(DOCUMENTS)
            .where(d.tenant_id == tenant, d.status == NOT_INDEXED, d.classification.in_(allowed))
            .values(status=PENDING, status_reason="Now allowed, and waiting to be indexed")
            .returning(d.document_id, d.revision)
        )
    ).all()  # fmt: skip
    for row in now_allowed:
        await _enqueue(connection, tenant, row.document_id, row.revision, now)
    await audit(connection, tenant_id=tenant, actor_type=actor_type, actor_id=actor,
                action="knowledge_settings.changed", resource_type="knowledge_settings",
                resource_id=tenant)  # fmt: skip
    return Allowance(classification, now, actor)


# --- indexing


def _retry_after(attempts: int) -> timedelta:
    return timedelta(seconds=min(FIRST_RETRY_SECONDS * 2 ** max(attempts - 1, 0),
                                 LONGEST_RETRY_SECONDS))  # fmt: skip


async def _take(engine: AsyncEngine, now: datetime) -> Any:
    """The next due revision, leased until its next attempt, so no other worker takes it."""
    q = INDEX_QUEUE.c
    async with engine.begin() as connection:
        entry = (
            await connection.execute(
                select(INDEX_QUEUE).where(q.next_attempt_at <= now)
                .order_by(q.next_attempt_at).limit(1).with_for_update(skip_locked=True)
            )
        ).one_or_none()  # fmt: skip
        if entry is None:
            return None
        attempts = entry.attempts + 1
        await connection.execute(
            update(INDEX_QUEUE)
            .where(q.tenant_id == entry.tenant_id, q.document_id == entry.document_id,
                   q.revision == entry.revision)
            .values(attempts=attempts, next_attempt_at=now + _retry_after(attempts))
        )  # fmt: skip
        return entry


async def _finish(connection: AsyncConnection, entry: Any, **values: Any) -> None:
    q, d = INDEX_QUEUE.c, DOCUMENTS.c
    await connection.execute(
        delete(INDEX_QUEUE).where(q.tenant_id == entry.tenant_id,
                                  q.document_id == entry.document_id,
                                  q.revision == entry.revision)
    )  # fmt: skip
    if values:
        await connection.execute(
            update(DOCUMENTS)
            .where(d.tenant_id == entry.tenant_id, d.document_id == entry.document_id,
                   d.revision == entry.revision)
            .values(**values)
        )  # fmt: skip


def embedding_input(title: str, section: str, text: str) -> str:
    """What is embedded: the passage under its document's title and section, as it is read."""
    return "\n".join(part for part in (title, section) if part) + "\n\n" + text


async def index_next(engine: AsyncEngine, embedder: embeddings.Embedder, store: vectors.VectorStore,
                     now: datetime) -> bool:  # fmt: skip
    """Index the next due revision. Return whether there was one."""
    entry = await _take(engine, now)
    if entry is None:
        return False
    tenant, resource = entry.tenant_id, f"{entry.document_id}@{entry.revision}"
    async with engine.connect() as connection:
        await in_tenant(connection, tenant)
        try:
            row = await document(connection, tenant, entry.document_id, entry.revision)
        except NotFoundError:
            row = None
        allowed = (await allowance(connection, tenant)).embedding_max_classification
        p = PASSAGES.c
        found = (
            await connection.execute(
                select(PASSAGES).where(p.tenant_id == tenant, p.document_id == entry.document_id,
                                       p.revision == entry.revision).order_by(p.ordinal)
            )
        ).all()  # fmt: skip
    if row is None or row.status != PENDING:
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            await _finish(connection, entry)
        return True
    if rank(row.classification) > rank(allowed):
        # The allowance was lowered after it was queued: it is not sent.
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            await _finish(
                connection,
                entry,
                status=NOT_INDEXED,
                status_reason=_not_indexed_reason(row.classification, allowed),
            )
        return True
    try:
        embedded = await embedder.embed(
            [embedding_input(row.title, x.section, x.text) for x in found]
        )
        collection = vectors.collection_for(embedder.model, embedder.dimensions)
        await store.ensure_collection(collection, embedder.dimensions)
        payload = {
            "classification": row.classification, "type": row.type,
            "applies_to": json.loads(row.applies_to), "published": row.published,
            "embedding_provider": embedder.provider, "embedding_model": embedded.model,
            "embedding_version": embedder.version, "dimensions": embedder.dimensions,
            "passages_version": passages.VERSION,
        }  # fmt: skip
        await store.upsert(collection, [
            vectors.Point(tenant, row.document_id, row.revision, x.ordinal, vector,
                          {**payload, "content_hash": x.content_hash})
            for x, vector in zip(found, embedded.vectors, strict=True)
        ])  # fmt: skip
    except (embeddings.EmbeddingUnavailableError, vectors.VectorsUnavailableError) as error:
        logger.warning("Indexing paused", extra={"error_type": type(error).__name__,
                                                  "attempts": entry.attempts + 1})  # fmt: skip
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            d = DOCUMENTS.c
            await connection.execute(
                update(DOCUMENTS)
                .where(d.tenant_id == tenant, d.document_id == row.document_id,
                       d.revision == row.revision)
                .values(status_reason=f"Waiting to be indexed: {error}")
            )  # fmt: skip
        return True
    except embeddings.EmbeddingRefusedError as error:
        async with engine.begin() as connection:
            await in_tenant(connection, tenant)
            await _finish(connection, entry, status=FAILED, status_reason=str(error))
            await audit(connection, tenant_id=tenant, actor_type=SERVICE, actor_id=SERVICE,
                        action="knowledge_document.indexed", resource_id=resource,
                        outcome="failed")  # fmt: skip
        return True
    async with engine.begin() as connection:
        await in_tenant(connection, tenant)
        await _finish(
            connection, entry, status=INDEXED, status_reason="",
            embedding_provider=embedder.provider, embedding_model=embedded.model,
            embedding_version=embedder.version, indexed_at=now,
        )  # fmt: skip
        await audit(connection, tenant_id=tenant, actor_type=SERVICE, actor_id=SERVICE,
                    action="knowledge_document.indexed", resource_id=resource)  # fmt: skip
    logger.info("Document indexed", extra={"passages": len(found), "tokens": embedded.tokens})
    return True
