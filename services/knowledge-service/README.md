# windops-knowledge

Approved manuals, bulletins, procedures and engineering documents, ingested for each tenant (SRS 6.2, FR-KNW-001,
FR-KNW-002), and indexed through the embedding adapter into Qdrant (LLM-011,
[ADR 0011](../../docs/adr/0011-openai-embeddings-through-the-adapter.md)), and searched for citable passages, each
checked against the caller's permissions before its text is returned (FR-KNW-003 to FR-KNW-005,
[ADR 0012](../../docs/adr/0012-document-permissions-and-current-revisions.md)).

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

## Retrieval

`SearchPassages` takes a question, and gives the passages that answer it, best first:

| Step | What happens |
| --- | --- |
| The query | Up to 1,000 characters. One holding a credential is refused before it is sent to be embedded, with the model the passages were |
| The tenant | Qdrant searches the caller's verified tenant only, a filter the request cannot change (DAT-006). PostgreSQL's row-level security applies it again when the text is read |
| What was indexed | A passage is returned only if its document is indexed, and its text's hash is the one its point was made from |
| Permission | Each document's comes from the Authorization Service (FR-KNW-004). A site document needs `knowledge.read` for one of its wind farms. A manufacturer document needs it where the search is made: the tenant, or the `wind_farm_id` the request names. A passage that fails is dropped, unannounced |
| Current revisions | A revision replaced by a newer one of its document, or by a document that supersedes it, is left out unless `include_superseded` is set, and then marked |

Each passage comes with its citation, `kn:<document>@<revision>#<passage>`, its document's metadata, its section, its
text, its offsets and lines, its hash, whether it is current and what replaced it, and the embedding it was found by.
Filters narrow a search to what documents apply to, and their types.

- **A citation stays valid:** `GetPassage` reads one passage as it was, current or not, if the caller may read its
  document. Otherwise it is not found.
- **Replacing a revision is announced:** indexing a revision that replaces another publishes `evidence.changed`, the
  replaced document and what replaced it, for the stale-evidence rule on procedures (SRS 8.5).
- **Re-indexing:** `ReindexDocuments`, with `knowledge.manage` and a second factor, queues a document's indexed and
  failed revisions, or the whole tenant's, to be embedded again (FR-KNW-005). Each is embedded under the same point
  IDs, and stays searchable meanwhile. Its passages and citations never change; only the embedding it records does.
- **When a dependency is down:** without the embedding provider, Qdrant or the Authorization Service, a search is
  unavailable. It never answers with passages that were not ranked and checked (SRS 7.7).

## Browsing the library

The console's knowledge library (M11.5) reads through `ListDocuments`, `GetDocument` and `ListPassages`, under the
same permission as a search:

- **Where it is read:** each takes an optional `wind_farm_id`. Without one, the caller needs `knowledge.read` for the
  whole tenant; with one, for that wind farm.
- **Each document:** a site document is listed and read only by someone who may read one of its wind farms. One they
  may not is left out of a list, and not found when asked for, so they never learn it exists. A list page may then hold
  fewer than asked for; its token still moves past every document looked at.
- **Reading a document:** `ListPassages` gives one revision's passages in order, each with the citation `GetPassage`
  reads, and marks them replaced when a newer revision is indexed. Held and unindexed revisions are read the same way,
  so a person can review a held one before releasing it.
- **The ingestion settings:** read with `knowledge.read` or `knowledge.manage`, since a Tenant Administrator manages
  the library without reading it, and changes this setting.

## The demo documents

`make knowledge-seed` ingests the synthetic document set's demo documents (M7.1) for the tenants its manifest names.
It takes the same checks and ingestion as an upload, as the actor `seed`, and is safe to repeat. The demo tenants hold
only synthetic documents, so the seed lets them send restricted ones too. The document carrying a prompt injection is
evaluation material, and never loaded here.

## Run the tests

```bash
uv run pytest services/knowledge-service/tests
make knowledge-check   # on the cluster: uploads, holds, real embeddings in Qdrant, and PostgreSQL's protections
make retrieval-check   # on the cluster: real searches, tenants, scoped permissions, revisions and re-indexing
```

## Build the image

```bash
docker build -f services/knowledge-service/Dockerfile -t windops-knowledge:local .
```
