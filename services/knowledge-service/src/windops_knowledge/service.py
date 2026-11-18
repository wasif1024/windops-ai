"""The gRPC API: ingesting documents, reading what was ingested, and the ingestion settings.

Every call asks Authorization first, and stays in the caller's tenant. Reading needs
knowledge.read; every change needs knowledge.manage, a person and a recent second factor, and is
audited. A change with an idempotency key returns its first answer when retried.
"""

import base64
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.authorization.v1 import authorization_pb2
from windops.knowledge.v1 import knowledge_pb2 as pb
from windops.knowledge.v1 import knowledge_pb2_grpc
from windops_auth import (
    STEP_UP_MESSAGE,
    MfaRequiredError,
    authentication_claims,
    require_recent_mfa,
)
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    PermissionDeniedError,
    ServiceClient,
    UnavailableError,
    current_identity,
)
from windops_migrations import in_tenant
from windops_resilience import BreakerOpenError

from windops_knowledge import library, uploads
from windops_knowledge.store import DOCUMENTS, PASSAGES, KeyReusedError, remember, remembered

READ, MANAGE = "knowledge.read", "knowledge.manage"
DEFAULT_PAGE, MAXIMUM_PAGE = 50, 200
MAXIMUM_KEY, MAXIMUM_TEXT = 128, 500
DEADLINE_SECONDS = 3.0
STATUSES = (library.PENDING, library.INDEXED, library.HELD, library.NOT_INDEXED, library.FAILED)


class Authorizer(Protocol):
    async def allowed(self, permission: str) -> bool: ...


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str) -> bool:
        request = authorization_pb2.CheckRequest(permission=permission)
        try:
            answer = await self._client.call(self._stub.Check, request, idempotent=True,
                                             deadline_seconds=DEADLINE_SECONDS)  # fmt: skip
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)


def _stamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def to_message(row: Any, passages: int) -> pb.DocumentVersion:
    return pb.DocumentVersion(
        metadata=pb.DocumentMetadata(
            document_id=row.document_id, revision=row.revision, title=row.title, type=row.type,
            published=row.published, issued_by=row.issued_by,
            applies_to=json.loads(row.applies_to), classification=row.classification,
            supersedes_document_id=row.supersedes_document_id,
            supersedes_revision=row.supersedes_revision,
        ),
        source=row.source, filename=row.filename, content_type=row.content_type,
        size_bytes=row.size_bytes, content_hash=row.content_hash,
        ingested_at=_stamp(row.ingested_at), ingested_by=row.ingested_by, status=row.status,
        status_reason=row.status_reason, passages=passages,
        embedding_provider=row.embedding_provider, embedding_model=row.embedding_model,
        embedding_version=row.embedding_version, indexed_at=_stamp(row.indexed_at),
        metadata_from_document=json.loads(row.metadata_from_document),
        screening=[pb.ScreeningFinding(**f) for f in json.loads(row.screening)],
        released_by=row.released_by,
    )  # fmt: skip


def requested_metadata(given: pb.DocumentMetadata) -> uploads.Metadata:
    return uploads.Metadata(
        document_id=given.document_id, revision=given.revision, title=given.title,
        type=given.type, published=given.published, issued_by=given.issued_by,
        applies_to=tuple(given.applies_to), classification=given.classification,
        supersedes_document_id=given.supersedes_document_id,
        supersedes_revision=given.supersedes_revision,
    )  # fmt: skip


async def passage_count(connection: AsyncConnection, tenant: str, document_id: str,
                        revision: int) -> int:  # fmt: skip
    p = PASSAGES.c
    found = await connection.execute(
        select(func.count()).select_from(PASSAGES).where(
            p.tenant_id == tenant, p.document_id == document_id, p.revision == revision)
    )  # fmt: skip
    return int(found.scalar_one())


def _settings(allowance: library.Allowance) -> pb.IngestionSettings:
    return pb.IngestionSettings(
        embedding_max_classification=allowance.embedding_max_classification,
        updated_at=_stamp(allowance.updated_at), updated_by=allowance.updated_by,
    )  # fmt: skip


class KnowledgeService(knowledge_pb2_grpc.KnowledgeServiceServicer):
    def __init__(self, engine_: AsyncEngine, authorizer: Authorizer,
                 clock: Callable[[], datetime] | None = None) -> None:  # fmt: skip
        self._engine, self._authorizer = engine_, authorizer
        self._now = clock or (lambda: datetime.now(UTC))

    # --- who may do what

    async def _require_read(self) -> str:
        if not await self._authorizer.allowed(READ):
            raise PermissionDeniedError("You do not have permission to do this")
        return current_identity().tenant_id

    async def _require_manage(self) -> tuple[str, str]:
        """Only a person, with knowledge.manage and a recent second factor (SEC-003)."""
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a person can change the knowledge library")
        if not await self._authorizer.allowed(MANAGE):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP_MESSAGE) from error
        return identity.tenant_id, identity.user_id

    async def _change(
        self, method: str, request: Message, key: str, response_type: Any,
        act: Callable[[AsyncConnection, str, str, datetime], Awaitable[Message]],
    ) -> Any:  # fmt: skip
        if len(key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant, user = await self._require_manage()
        now = self._now()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                try:
                    first = await remembered(connection, tenant, user, key, method, request)
                except KeyReusedError as error:
                    raise InvalidRequestError(str(error)) from error
                if first is not None:
                    return response_type.FromString(first)
                response = await act(connection, tenant, user, now)
                await remember(
                    connection, tenant_id=tenant, user_id=user, key=key, method=method,
                    request=request, response=response, now=now,
                )  # fmt: skip
        except IntegrityError as error:
            raise ConflictError(
                "Someone else changed this document first. Read it, and try again"
            ) from error
        return response

    # --- documents

    async def UploadDocument(  # noqa: N802  # The name comes from the contract.
        self, request: pb.UploadDocumentRequest, context: Any
    ) -> pb.UploadDocumentResponse:
        source = request.source.strip()
        if not source or len(source) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"source is required, and at most {MAXIMUM_TEXT} characters")
        given = requested_metadata(request.metadata) if request.HasField("metadata") else None
        try:
            upload = uploads.prepare(request.filename, request.content_type, request.content,
                                     given)  # fmt: skip
        except uploads.UploadRefusedError as error:
            raise InvalidRequestError(str(error)) from error

        async def act(connection: AsyncConnection, tenant: str, user: str, now: datetime) -> Any:
            row, _ = await library.ingest(
                connection, tenant, "user", user, upload, filename=request.filename,
                content_type=request.content_type, source=source, now=now,
            )  # fmt: skip
            count = await passage_count(connection, tenant, row.document_id, row.revision)
            return pb.UploadDocumentResponse(document=to_message(row, count))

        response: pb.UploadDocumentResponse = await self._change(
            "UploadDocument", request, request.idempotency_key, pb.UploadDocumentResponse, act
        )
        return response

    async def GetDocument(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetDocumentRequest, context: Any
    ) -> pb.GetDocumentResponse:
        tenant = await self._require_read()
        if request.revision < 0:
            raise InvalidRequestError("revision is 0, for the newest, or more")
        d = DOCUMENTS.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row = await library.document(connection, tenant, request.document_id,
                                         request.revision)  # fmt: skip
            count = await passage_count(connection, tenant, row.document_id, row.revision)
            revisions = (
                await connection.execute(
                    select(d.revision).where(d.tenant_id == tenant,
                                             d.document_id == request.document_id)
                    .order_by(d.revision)
                )
            ).scalars().all()  # fmt: skip
        return pb.GetDocumentResponse(document=to_message(row, count), revisions=list(revisions))

    async def ListDocuments(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListDocumentsRequest, context: Any
    ) -> pb.ListDocumentsResponse:
        tenant = await self._require_read()
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        if request.status and request.status not in STATUSES:
            raise InvalidRequestError(f"status is one of {', '.join(STATUSES)}")
        after = ""
        if request.page_token:
            try:
                after = base64.urlsafe_b64decode(request.page_token.encode()).decode()
            except (ValueError, UnicodeDecodeError) as error:
                raise InvalidRequestError("page_token is not one this service gave") from error
        d = DOCUMENTS.c
        newest = (
            select(d.document_id, func.max(d.revision).label("revision"))
            .where(d.tenant_id == tenant).group_by(d.document_id).subquery()
        )  # fmt: skip
        query = (
            select(DOCUMENTS)
            .join(newest, (d.document_id == newest.c.document_id)
                  & (d.revision == newest.c.revision))
            .where(d.tenant_id == tenant, d.document_id > after)
            .order_by(d.document_id).limit(size + 1)
        )  # fmt: skip
        if request.status:
            query = query.where(d.status == request.status)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(query)).all()
            documents = [
                to_message(row, await passage_count(connection, tenant, row.document_id,
                                                    row.revision))
                for row in rows[:size]
            ]  # fmt: skip
        token = ""
        if len(rows) > size:
            token = base64.urlsafe_b64encode(rows[size - 1].document_id.encode()).decode()
        return pb.ListDocumentsResponse(documents=documents, next_page_token=token)

    async def ReleaseDocument(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ReleaseDocumentRequest, context: Any
    ) -> pb.ReleaseDocumentResponse:
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")

        async def act(connection: AsyncConnection, tenant: str, user: str, now: datetime) -> Any:
            row = await library.release(connection, tenant, user, request.document_id,
                                        request.revision, now)  # fmt: skip
            count = await passage_count(connection, tenant, row.document_id, row.revision)
            return pb.ReleaseDocumentResponse(document=to_message(row, count))

        response: pb.ReleaseDocumentResponse = await self._change(
            "ReleaseDocument", request, request.idempotency_key, pb.ReleaseDocumentResponse, act
        )
        return response

    # --- settings

    async def GetIngestionSettings(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetIngestionSettingsRequest, context: Any
    ) -> pb.GetIngestionSettingsResponse:
        tenant = await self._require_read()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            found = await library.allowance(connection, tenant)
        return pb.GetIngestionSettingsResponse(settings=_settings(found))

    async def SetIngestionSettings(  # noqa: N802  # The name comes from the contract.
        self, request: pb.SetIngestionSettingsRequest, context: Any
    ) -> pb.SetIngestionSettingsResponse:
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")

        async def act(connection: AsyncConnection, tenant: str, user: str, now: datetime) -> Any:
            wanted = request.embedding_max_classification
            changed = await library.set_allowance(connection, tenant, "user", user, wanted, now)
            return pb.SetIngestionSettingsResponse(settings=_settings(changed))

        response: pb.SetIngestionSettingsResponse = await self._change(
            "SetIngestionSettings", request, request.idempotency_key,
            pb.SetIngestionSettingsResponse, act,
        )  # fmt: skip
        return response
