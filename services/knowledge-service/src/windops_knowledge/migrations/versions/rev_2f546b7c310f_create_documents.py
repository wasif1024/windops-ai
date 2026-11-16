"""Create the documents, passages, ingestion settings, index queue and idempotency keys.

A document's content and metadata never change once kept, nor do its passages: a citation must
always find what it cited (FR-KNW-005). Only its indexing state moves.

Revision ID: 2f546b7c310f
"""

import sqlalchemy as sa
from alembic import op
from windops_migrations import enable_tenant_isolation

revision: str = "2f546b7c310f"
down_revision: str | None = None
# "expand" keeps the database working for the running version. Anything that would break it,
# such as dropping a column, is "contract", and belongs in a later release (DAT-001).
phase: str = "expand"


def upgrade() -> None:
    # Written out, not built from the code's tables, so this revision never changes.
    op.create_table(
        "documents",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("document_id", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("published", sa.Text(), nullable=False),
        sa.Column("issued_by", sa.Text(), nullable=False),
        sa.Column("applies_to", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("supersedes_document_id", sa.Text(), nullable=False),
        sa.Column("supersedes_revision", sa.Integer(), nullable=False),
        sa.Column("metadata_from_document", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_by", sa.Text(), nullable=False),
        sa.Column("screening", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=False),
        sa.Column("released_by", sa.Text(), nullable=False),
        sa.Column("embedding_provider", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_version", sa.Text(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'indexed', 'held', 'not_indexed', 'failed')",
            name="documents_status",
        ),
        sa.CheckConstraint(
            "classification IN ('public', 'internal', 'confidential', 'restricted')",
            name="documents_classification",
        ),
    )
    op.create_index("documents_by_status", "documents", ["tenant_id", "status"])
    op.create_table(
        "passages",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("document_id", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("first_line", sa.Integer(), nullable=False),
        sa.Column("last_line", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id", "revision"],
            ["documents.tenant_id", "documents.document_id", "documents.revision"],
        ),
    )
    op.create_table(
        "ingestion_settings",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("embedding_max_classification", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
    )
    # The service's own work list, across tenants, as the outbox is: identifiers only.
    op.create_table(
        "index_queue",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("document_id", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("index_queue_due", "index_queue", ["next_attempt_at"])
    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # What a document says, and who sent it, never changes; only its indexing state does.
    op.execute(
        "CREATE FUNCTION documents_keep_content() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'A kept document is never deleted'; END IF; "
        "IF (NEW.tenant_id, NEW.document_id, NEW.revision, NEW.title, NEW.type, NEW.published, "
        "NEW.issued_by, NEW.applies_to, NEW.classification, NEW.supersedes_document_id, "
        "NEW.supersedes_revision, NEW.metadata_from_document, NEW.source, NEW.filename, "
        "NEW.content_type, NEW.content, NEW.size_bytes, NEW.content_hash, NEW.ingested_at, "
        "NEW.ingested_by, NEW.screening) IS DISTINCT FROM (OLD.tenant_id, OLD.document_id, "
        "OLD.revision, OLD.title, OLD.type, OLD.published, OLD.issued_by, OLD.applies_to, "
        "OLD.classification, OLD.supersedes_document_id, OLD.supersedes_revision, "
        "OLD.metadata_from_document, OLD.source, OLD.filename, OLD.content_type, OLD.content, "
        "OLD.size_bytes, OLD.content_hash, OLD.ingested_at, OLD.ingested_by, OLD.screening) "
        "THEN RAISE EXCEPTION 'A kept document never changes; only its indexing state does'; "
        "END IF; RETURN NEW; END $$"
    )
    op.execute(
        "CREATE TRIGGER documents_keep_content BEFORE UPDATE OR DELETE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION documents_keep_content()"
    )
    op.execute(
        "CREATE FUNCTION passages_never_change() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'A kept passage never changes'; END $$"
    )
    op.execute(
        "CREATE TRIGGER passages_never_change BEFORE UPDATE OR DELETE ON passages "
        "FOR EACH ROW EXECUTE FUNCTION passages_never_change()"
    )
    # DAT-005. The index queue holds no content, and is the service's own, across tenants.
    enable_tenant_isolation(op.get_bind(), "documents")
    enable_tenant_isolation(op.get_bind(), "passages")
    enable_tenant_isolation(op.get_bind(), "ingestion_settings")
    enable_tenant_isolation(op.get_bind(), "idempotency_keys")


def downgrade() -> None:
    raise NotImplementedError("To undo this, write a new forward migration")
