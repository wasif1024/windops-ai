# windops-knowledge

Approved manuals, bulletins, procedures and engineering documents, ingested for each tenant (SRS 6.2, FR-KNW-001,
FR-KNW-002), and indexed through the embedding adapter into Qdrant (LLM-011,
[ADR 0011](../../docs/adr/0011-openai-embeddings-through-the-adapter.md)). Retrieval, with citable passages and
permission checks, follows in M7.3.

## An upload

`UploadDocument` needs `knowledge.manage`, a person, and a recent second factor (SEC-003): Tenant Administrators and
Reliability Engineers. It is checked before anything is kept:

| Check | Refused when |
| --- | --- |
| Type | Not Markdown (`text/markdown`, `.md`) or plain text (`text/plain`, `.txt`), or the name and type disagree |
| Size | Empty, or over 1 MiB |
| Bytes | Not strict UTF-8, or holding control characters: whatever it calls itself, it is not text |
| Filename | Anything but letters, digits, dots, hyphens and underscores |
| Credentials | It holds a private key, an API key, a token or a password. The answer names what and the line, never the value |

## What a document says about itself (FR-KNW-002)

Its metadata comes from the header table under its title, as a controlled document states it:

- document ID, revision and title;
- type and publication date;
- who issued it, and the models or wind farms it applies to;
- its classification, and the revision it supersedes.

The request may give only what the header does not, such as a plain-text document's metadata. A field the two give
differently is refused, as one of them is wrong. Each kept revision records which fields came from the document, its
source, the uploader, when it was ingested, its size and the sha256 of its content.

A revision once kept never changes: the same revision uploaded again with the same content is the same upload, and
with other content it is refused. A database trigger refuses to change a document's content or metadata, or any
passage, even for the service's own login.

## Indexing

Each document is cut into passages, `passages-1`:
- A passage stays inside one section, under its headings, and runs to about 1,600 characters.
- A paragraph or table is never split.
- Each passage keeps its offsets, lines and hash, so a citation can be shown in place.

Then the revision is:

| Status | When |
| --- | --- |
| `pending` | Queued for the indexer |
| `indexed` | Its passages are in Qdrant, with the provider, model, adapter version and dimensions recorded on the document and on every point |
| `held` | It addresses instructions to an AI (LLM-008). A person other than its uploader reviews it, and `ReleaseDocument` sends it on |
| `not_indexed` | Its classification is above what the tenant sends to the embedding provider. It is kept, never sent |
| `failed` | The provider refused it, with the reason |

The indexer takes the queue one revision at a time. When the provider or Qdrant cannot be reached, the revision stays
pending and is tried again, backing off from 30 seconds to 30 minutes: ingestion pauses, and nothing is lost
(SRS 7.7). Qdrant holds only vectors and identifiers, never passage text, which stays in PostgreSQL.

## What goes to the embedding provider

- **Classification:** only passages at or below the tenant's `embedding_max_classification`, `confidential` by
  default. `SetIngestionSettings` changes it, with `knowledge.manage` and a second factor, audited. Raising it queues
  what it now allows. Lowering it stops what is still queued, but recalls nothing already sent.
- **Content:** the passage's text, under its document's title and section. Never a tenant, user or case.
- **Logging:** each request is logged by its count and size, never its text (EGR-004). TLS is always verified.
- **The key:** `OPENAI_API_KEY` in `.env`. `make secrets` makes it the Secret `windops-openai-api-key`, mounted as a
  file and read at each request, so a replaced key needs no restart. Without one, indexing waits.

## The demo documents

`make knowledge-seed` ingests the synthetic document set's demo documents (M7.1) for the tenants its manifest names.
It takes the same checks and ingestion as an upload, as the actor `seed`, and is safe to repeat. The demo tenants hold
only synthetic documents, so the seed lets them send restricted ones too. The document carrying a prompt injection is
evaluation material, and never loaded here.

## Run the tests

```bash
uv run pytest services/knowledge-service/tests
make knowledge-check   # on the cluster: uploads, holds, real embeddings in Qdrant, and PostgreSQL's protections
```

## Build the image

```bash
docker build -f services/knowledge-service/Dockerfile -t windops-knowledge:local .
```
