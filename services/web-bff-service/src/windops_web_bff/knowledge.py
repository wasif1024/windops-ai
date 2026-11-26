"""The knowledge library: browsing and reading documents, searching them, and managing them (M11.5).

It decides nothing. The Knowledge Service checks every call as the signed-in user: reading needs
knowledge.read where it is read, and a document that applies to wind farms is read only by someone
who may read one of them (FR-KNW-004). Uploading, releasing a held document, indexing again and
changing what may be embedded need knowledge.manage, a person and a recent second factor, and the
service screens every upload before anything is kept.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from windops.knowledge.v1 import knowledge_pb2 as kn

from windops_web_bff.common import Call, Key, time_of
from windops_web_bff.tokens import Caller

# As the Knowledge Service accepts them; it checks again.
DOCUMENT_ID = r"^[A-Z0-9][A-Z0-9-]{2,63}$"
WIND_FARM = r"^[a-z0-9][a-z0-9-]{1,62}$"
CITATION = r"^kn:[A-Z0-9][A-Z0-9-]{2,63}@\d{1,4}#\d{1,5}$"
STATUSES = r"^(pending|indexed|held|not_indexed|failed)$"
CLASSIFICATIONS = r"^(public|internal|confidential|restricted)$"
FILENAME = r"^[A-Za-z0-9._-]{1,128}\.(md|txt)$"
MAXIMUM_BYTES = 1024 * 1024

DocumentId = Annotated[str, Path(pattern=DOCUMENT_ID)]
WindFarm = Annotated[str, Query(pattern=WIND_FARM, description="Where it is read; empty for the "
                                "whole tenant")]  # fmt: skip
Revision = Annotated[int, Query(ge=0, le=9999, description="0 for the newest")]


class Metadata(BaseModel):
    document_id: str
    revision: int
    title: str
    # "manual", "bulletin", "procedure", "reference" or "engineering".
    type: str
    published: str
    issued_by: str
    applies_to: list[str]
    # "public", "internal", "confidential" or "restricted".
    classification: str
    supersedes_document_id: str
    supersedes_revision: int


class Finding(BaseModel):
    kind: str
    description: str
    line: int


class Document(BaseModel):
    metadata: Metadata
    source: str
    filename: str
    content_type: str
    size_bytes: int
    content_hash: str
    ingested_at: str | None
    ingested_by: str
    # "pending", "indexed", "held", "not_indexed" or "failed".
    status: str
    status_reason: str
    passages: int
    embedding_provider: str
    embedding_model: str
    embedding_version: str
    indexed_at: str | None
    metadata_from_document: list[str]
    # What the screening found, never the matched text.
    screening: list[Finding]
    released_by: str


class DocumentPage(BaseModel):
    documents: list[Document]
    next_page_token: str


class DocumentRevisions(BaseModel):
    document: Document
    revisions: list[int]


class Passage(BaseModel):
    # Such as "kn:WS-PRC-GBX-012@3#4", stable for ever.
    citation_id: str
    document: Metadata
    source: str
    section: str
    text: str
    first_line: int
    last_line: int
    content_hash: str
    current: bool
    superseded_by: str
    embedding_model: str
    embedding_version: str
    score: float


class DocumentText(BaseModel):
    document: Document
    passages: list[Passage]


class SearchResult(BaseModel):
    passages: list[Passage]
    embedding_model: str
    embedding_version: str


class Settings(BaseModel):
    embedding_max_classification: str
    updated_at: str | None
    updated_by: str


class UploadIn(BaseModel):
    filename: str = Field(pattern=FILENAME)
    content_type: str = Field(pattern=r"^text/(markdown|plain)$")
    # The text itself: only Markdown and plain text are accepted.
    content: str = Field(min_length=1, max_length=MAXIMUM_BYTES)
    source: str = Field(min_length=1, max_length=500)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ReindexIn(ReasonIn):
    # Empty for every document of the tenant.
    document_id: str = Field(default="", pattern=r"^([A-Z0-9][A-Z0-9-]{2,63})?$")


class Reindexed(BaseModel):
    queued: int


class SettingsIn(ReasonIn):
    embedding_max_classification: str = Field(pattern=CLASSIFICATIONS)


def _metadata(m: Any) -> Metadata:
    return Metadata(
        document_id=m.document_id, revision=m.revision, title=m.title, type=m.type,
        published=m.published, issued_by=m.issued_by, applies_to=list(m.applies_to),
        classification=m.classification, supersedes_document_id=m.supersedes_document_id,
        supersedes_revision=m.supersedes_revision,
    )  # fmt: skip


def _document(d: Any) -> Document:
    return Document(
        metadata=_metadata(d.metadata), source=d.source, filename=d.filename,
        content_type=d.content_type, size_bytes=d.size_bytes, content_hash=d.content_hash,
        ingested_at=time_of(d, "ingested_at"), ingested_by=d.ingested_by, status=d.status,
        status_reason=d.status_reason, passages=d.passages,
        embedding_provider=d.embedding_provider, embedding_model=d.embedding_model,
        embedding_version=d.embedding_version, indexed_at=time_of(d, "indexed_at"),
        metadata_from_document=list(d.metadata_from_document),
        screening=[Finding(kind=f.kind, description=f.description, line=f.line)
                   for f in d.screening],
        released_by=d.released_by,
    )  # fmt: skip


def _passage(p: Any) -> Passage:
    return Passage(
        citation_id=p.citation_id, document=_metadata(p.document), source=p.source,
        section=p.section, text=p.text, first_line=p.first_line, last_line=p.last_line,
        content_hash=p.content_hash, current=p.current, superseded_by=p.superseded_by,
        embedding_model=p.embedding_model, embedding_version=p.embedding_version, score=p.score,
    )  # fmt: skip


def _settings(s: Any) -> Settings:
    return Settings(embedding_max_classification=s.embedding_max_classification,
                    updated_at=time_of(s, "updated_at"), updated_by=s.updated_by)  # fmt: skip


def add_knowledge_routes(
    router: APIRouter, caller: Callable[..., Awaitable[Caller]], call_as: Call
) -> None:
    Who = Annotated[Caller, Depends(caller)]  # noqa: N806  # A type, named as one.

    @router.get(
        "/knowledge/documents", response_model=DocumentPage,
        summary="The library: each document's newest revision the user may read, by ID",
    )  # fmt: skip
    async def documents(
        who: Who,
        wind_farm_id: WindFarm = "",
        status: Annotated[str, Query(pattern=STATUSES)] = "",
        page_size: Annotated[int, Query(ge=1, le=200)] = 50,
        page_token: Annotated[str, Query(max_length=200)] = "",
    ) -> DocumentPage:
        request = kn.ListDocumentsRequest(page_size=page_size, page_token=page_token,
                                          status=status, wind_farm_id=wind_farm_id)  # fmt: skip
        answer = await call_as("knowledge", "ListDocuments", request, who)
        return DocumentPage(documents=[_document(d) for d in answer.documents],
                            next_page_token=answer.next_page_token)  # fmt: skip

    @router.get(
        "/knowledge/documents/{document_id}", response_model=DocumentRevisions,
        summary="One revision of a document, the newest by default, and the revisions kept",
    )  # fmt: skip
    async def document(
        document_id: DocumentId, who: Who, revision: Revision = 0, wind_farm_id: WindFarm = ""
    ) -> DocumentRevisions:
        request = kn.GetDocumentRequest(document_id=document_id, revision=revision,
                                        wind_farm_id=wind_farm_id)  # fmt: skip
        answer = await call_as("knowledge", "GetDocument", request, who)
        return DocumentRevisions(document=_document(answer.document),
                                 revisions=list(answer.revisions))  # fmt: skip

    @router.get(
        "/knowledge/documents/{document_id}/passages", response_model=DocumentText,
        summary="One revision's passages in order, each with its citation, to read it",
    )  # fmt: skip
    async def passages(
        document_id: DocumentId, who: Who, revision: Revision = 0, wind_farm_id: WindFarm = ""
    ) -> DocumentText:
        request = kn.ListPassagesRequest(document_id=document_id, revision=revision,
                                         wind_farm_id=wind_farm_id)  # fmt: skip
        answer = await call_as("knowledge", "ListPassages", request, who)
        return DocumentText(document=_document(answer.document),
                            passages=[_passage(p) for p in answer.passages])  # fmt: skip

    @router.get(
        "/knowledge/search", response_model=SearchResult,
        summary="Passages that answer a question, each with its citation, from documents the "
        "user may read",
    )  # fmt: skip
    async def search(
        who: Who,
        q: Annotated[str, Query(min_length=1, max_length=1000)],
        wind_farm_id: WindFarm = "",
        applies_to: Annotated[str, Query(max_length=64)] = "",
        types: Annotated[
            list[str] | None,
            Query(
                max_length=5, description="manual, bulletin, procedure, reference or engineering"
            ),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
        include_superseded: bool = False,
    ) -> SearchResult:
        request = kn.SearchPassagesRequest(
            query=q, wind_farm_id=wind_farm_id, applies_to=applies_to, types=types or [],
            limit=limit, include_superseded=include_superseded,
        )  # fmt: skip
        answer = await call_as("knowledge", "SearchPassages", request, who)
        return SearchResult(passages=[_passage(p) for p in answer.passages],
                            embedding_model=answer.embedding_model,
                            embedding_version=answer.embedding_version)  # fmt: skip

    @router.get(
        "/knowledge/passage", response_model=Passage,
        summary="One cited passage, as it was, even once its revision is replaced",
    )  # fmt: skip
    async def passage(
        who: Who,
        citation_id: Annotated[str, Query(pattern=CITATION)],
        wind_farm_id: WindFarm = "",
    ) -> Passage:
        request = kn.GetPassageRequest(citation_id=citation_id, wind_farm_id=wind_farm_id)
        return _passage((await call_as("knowledge", "GetPassage", request, who)).passage)

    @router.post(
        "/knowledge/documents", response_model=Document, status_code=201,
        summary="Upload one revision of a document: screened first, and held for someone else "
        "to release when it addresses an AI",
    )  # fmt: skip
    async def upload(body: UploadIn, key: Key, who: Who) -> Document:
        request = kn.UploadDocumentRequest(
            filename=body.filename, content_type=body.content_type,
            content=body.content.encode(), source=body.source, idempotency_key=key,
        )  # fmt: skip
        answer = await call_as("knowledge", "UploadDocument", request, who, retry=False)
        return _document(answer.document)

    @router.post(
        "/knowledge/documents/{document_id}/revisions/{revision}/release",
        response_model=Document,
        summary="Release a held revision to be indexed. Never by the person who uploaded it",
    )  # fmt: skip
    async def release(
        document_id: DocumentId,
        revision: Annotated[int, Path(ge=1, le=9999)],
        body: ReasonIn, key: Key, who: Who,
    ) -> Document:  # fmt: skip
        request = kn.ReleaseDocumentRequest(document_id=document_id, revision=revision,
                                            reason=body.reason, idempotency_key=key)  # fmt: skip
        answer = await call_as("knowledge", "ReleaseDocument", request, who, retry=False)
        return _document(answer.document)

    @router.post(
        "/knowledge/reindex", response_model=Reindexed,
        summary="Index again under the same citations: one document, or all of them",
    )  # fmt: skip
    async def reindex(body: ReindexIn, key: Key, who: Who) -> Reindexed:
        request = kn.ReindexDocumentsRequest(document_id=body.document_id, reason=body.reason,
                                             idempotency_key=key)  # fmt: skip
        answer = await call_as("knowledge", "ReindexDocuments", request, who, retry=False)
        return Reindexed(queued=answer.queued)

    @router.get(
        "/knowledge/settings", response_model=Settings,
        summary="The highest classification whose passages may go to the embedding provider",
    )  # fmt: skip
    async def settings(who: Who) -> Settings:
        request = kn.GetIngestionSettingsRequest()
        return _settings(
            (await call_as("knowledge", "GetIngestionSettings", request, who)).settings
        )

    @router.put(
        "/knowledge/settings", response_model=Settings,
        summary="Change what may be embedded. A document above it is kept, but not indexed",
    )  # fmt: skip
    async def change_settings(body: SettingsIn, key: Key, who: Who) -> Settings:
        request = kn.SetIngestionSettingsRequest(
            embedding_max_classification=body.embedding_max_classification, reason=body.reason,
            idempotency_key=key,
        )  # fmt: skip
        answer = await call_as("knowledge", "SetIngestionSettings", request, who, retry=False)
        return _settings(answer.settings)
