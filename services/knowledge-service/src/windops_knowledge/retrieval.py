"""Passages for a question, each citable, each checked before its text is returned (ADR 0012).

1. The query is checked for credentials, and embedded with the model the passages were.
2. Qdrant ranks the passages, in the caller's tenant only: the filter comes from the verified
   identity, never the request (DAT-006).
3. PostgreSQL gives each passage's text, under row-level security, with its document. A passage
   whose document is not indexed, or whose hash differs from its point's, is dropped.
4. Each document's permission comes from the Authorization Service (FR-KNW-004): a site document
   needs knowledge.read for one of its wind farms, and a manufacturer document needs it where the
   search is made. A document that fails is dropped, unannounced.
5. Superseded revisions are dropped, unless asked for; those kept are marked (FR-KNW-005).
"""

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection
from windops_screening import credentials_in

from windops_knowledge import embeddings, library, vectors
from windops_knowledge.store import DOCUMENTS, PASSAGES

READ = "knowledge.read"
MAXIMUM_QUERY = 1000
DEFAULT_LIMIT, MAXIMUM_LIMIT = 5, 20
# Asked of Qdrant beyond the limit, as some passages are dropped after it: superseded revisions,
# documents the caller may not read.
OVERFETCH = 4
CITATION = re.compile(r"kn:(?P<document>[A-Z0-9][A-Z0-9-]{2,63})@(?P<revision>\d{1,4})"
                      r"#(?P<ordinal>\d{1,5})")  # fmt: skip
# The Asset Service's rule for an ID. A document applying to a value of this form applies to a
# wind farm; any other value, such as WS-8.0-167, is a turbine model.
WIND_FARM = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")

Allowed = Callable[[str, str], Awaitable[bool]]


class QueryRefusedError(ValueError):
    """The query cannot be sent to be embedded, and why."""


class NoSuchCitationError(LookupError):
    """No passage the caller may read has that citation."""


@dataclass(frozen=True)
class Hit:
    document: Any
    passage: Any
    score: float
    superseded_by: str

    @property
    def citation_id(self) -> str:
        return citation_id(self.document.document_id, self.document.revision,
                           self.passage.ordinal)  # fmt: skip


def citation_id(document_id: str, revision: int, ordinal: int) -> str:
    return f"kn:{document_id}@{revision}#{ordinal}"


def parse_citation(value: str) -> tuple[str, int, int]:
    match = CITATION.fullmatch(value)
    if match is None:
        raise NoSuchCitationError("A citation is kn:<document>@<revision>#<passage>")
    return match["document"], int(match["revision"]), int(match["ordinal"])


def wind_farms(applies_to: Iterable[str]) -> list[str]:
    return [a for a in applies_to if WIND_FARM.fullmatch(a)]


def check_query(query: str) -> str:
    text = query.strip()
    if not text:
        raise QueryRefusedError("A query is needed")
    if len(text) > MAXIMUM_QUERY:
        raise QueryRefusedError(f"A query is at most {MAXIMUM_QUERY} characters")
    found = credentials_in(text)
    if found:
        raise QueryRefusedError(
            f"The query holds {found[0][0]}, so it is not sent to be embedded. Remove it"
        )
    return text


async def may_read(document: Any, wind_farm_id: str, allowed: Allowed,
                   decided: dict[tuple[str, str], bool]) -> bool:  # fmt: skip
    """The document permission, from the Authorization Service, each scope asked once."""

    async def ask(scope: str) -> bool:
        if (READ, scope) not in decided:
            decided[(READ, scope)] = await allowed(READ, scope)
        return decided[(READ, scope)]

    farms = wind_farms(json.loads(document.applies_to))
    if farms:
        for farm in farms:
            if await ask(farm):
                return True
        return False
    return await ask(wind_farm_id)


async def superseded(connection: AsyncConnection, tenant: str,
                     wanted: set[tuple[str, int]]) -> dict[tuple[str, int], str]:  # fmt: skip
    """What replaced each of these revisions, by its citation prefix, for those replaced.

    A revision is replaced by a newer indexed revision of its document, or by an indexed document
    that says it supersedes it, or a later revision of it.
    """
    if not wanted:
        return {}
    d = DOCUMENTS.c
    ids = sorted({document_id for document_id, _ in wanted})
    newest = dict(
        (
            await connection.execute(
                select(d.document_id, func.max(d.revision))
                .where(d.tenant_id == tenant, d.status == library.INDEXED, d.document_id.in_(ids))
                .group_by(d.document_id)
            )
        ).all()
    )  # fmt: skip
    replacing = (
        await connection.execute(
            select(d.document_id, d.revision, d.supersedes_document_id, d.supersedes_revision)
            .where(d.tenant_id == tenant, d.status == library.INDEXED,
                   d.supersedes_document_id.in_(ids))
        )
    ).all()  # fmt: skip
    found: dict[tuple[str, int], str] = {}
    for document_id, revision in wanted:
        if newest.get(document_id, revision) > revision:
            found[(document_id, revision)] = f"{document_id}@{newest[document_id]}"
            continue
        for row in replacing:
            if (row.supersedes_document_id == document_id and row.supersedes_revision >= revision
                    and (row.document_id, row.revision) != (document_id, revision)):  # fmt: skip
                found[(document_id, revision)] = f"{row.document_id}@{row.revision}"
                break
    return found


async def _rows(
    connection: AsyncConnection, tenant: str, keys: list[tuple[str, int, int]]
) -> dict[tuple[str, int, int], tuple[Any, Any]]:
    """Each passage, with its document, from PostgreSQL: the authority on both."""
    if not keys:
        return {}
    p, d = PASSAGES.c, DOCUMENTS.c
    passages = (
        await connection.execute(
            select(PASSAGES).where(
                p.tenant_id == tenant,
                or_(*(and_(p.document_id == k[0], p.revision == k[1], p.ordinal == k[2])
                      for k in keys)),
            )
        )
    ).all()  # fmt: skip
    revisions = {(row.document_id, row.revision) for row in passages}
    documents = {
        (row.document_id, row.revision): row
        for row in (
            await connection.execute(
                select(DOCUMENTS).where(
                    d.tenant_id == tenant,
                    or_(*(and_(d.document_id == i, d.revision == r) for i, r in revisions)),
                )
            )
        ).all()
    } if revisions else {}  # fmt: skip
    return {
        (row.document_id, row.revision, row.ordinal): (documents[(row.document_id, row.revision)],
                                                       row)
        for row in passages if (row.document_id, row.revision) in documents
    }  # fmt: skip


async def search(
    connection: AsyncConnection, tenant: str, query: str, *, embedder: embeddings.Embedder,
    store: vectors.VectorStore, allowed: Allowed, wind_farm_id: str = "", applies_to: str = "",
    types: tuple[str, ...] = (), limit: int = DEFAULT_LIMIT, include_superseded: bool = False,
) -> list[Hit]:  # fmt: skip
    text = check_query(query)
    vector = (await embedder.embed([text])).vectors[0]
    collection = vectors.collection_for(embedder.model, embedder.dimensions)
    found = await store.search(collection, vector, tenant, limit * OVERFETCH, applies_to, types)
    rows = await _rows(connection, tenant, [(f.document_id, f.revision, f.ordinal) for f in found])
    replaced = await superseded(connection, tenant, {(f.document_id, f.revision) for f in found})
    decided: dict[tuple[str, str], bool] = {}
    hits: list[Hit] = []
    for point in found:
        pair = rows.get((point.document_id, point.revision, point.ordinal))
        if pair is None:
            continue
        document, passage = pair
        # Only what was indexed, and only the text the point was made from.
        if document.status != library.INDEXED or \
                point.payload.get("content_hash") != passage.content_hash:  # fmt: skip
            continue
        by = replaced.get((document.document_id, document.revision), "")
        if by and not include_superseded:
            continue
        if not await may_read(document, wind_farm_id, allowed, decided):
            continue
        hits.append(Hit(document, passage, point.score, by))
        if len(hits) == limit:
            break
    return hits


async def read(connection: AsyncConnection, tenant: str, citation: str, *,
               wind_farm_id: str, allowed: Allowed) -> Hit:  # fmt: skip
    """One cited passage, as it was, current or not, if the caller may read its document."""
    document_id, revision, ordinal = parse_citation(citation)
    pair = (await _rows(connection, tenant, [(document_id, revision, ordinal)])).get(
        (document_id, revision, ordinal))  # fmt: skip
    if pair is None or not await may_read(pair[0], wind_farm_id, allowed, {}):
        raise NoSuchCitationError("No such passage")
    replaced = await superseded(connection, tenant, {(document_id, revision)})
    return Hit(pair[0], pair[1], 0.0, replaced.get((document_id, revision), ""))
