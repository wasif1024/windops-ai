# ADR 0012: Who may read a document, and which revision is current

**Status:** Accepted, 2026-11-18, for M7.3.

## Context

Retrieval must return passages suitable for citation (FR-KNW-003), enforce document permissions before any content
reaches an agent (FR-KNW-004), and support re-indexing and version replacement without losing provenance
(FR-KNW-005). Every Qdrant query needs a server-applied tenant filter, then a document-permission check (DAT-006).

The SRS defines `knowledge.read`, which may be scoped to wind farms and turbines, and an access classification on
every document. It doesn't say what a document permission is, or what makes a revision current. Both decide what an
agent may cite.

## Decision

**Who may read a document:**

- **The tenant:** only the caller's verified tenant is searched. Qdrant applies it as a filter that the request cannot
  change, and PostgreSQL's row-level security applies it again when the passage text is read.
- **What a document applies to:** a site document applies to wind farms, so reading it needs `knowledge.read` for one
  of them. A manufacturer document applies to turbine models, which may be anywhere in the tenant's fleet, so reading
  it needs `knowledge.read` where the search is made: for the whole tenant, or for the wind farm the request names.
- **Checked before any text is returned:** each document's permission comes from the Authorization Service, after
  Qdrant has ranked the passages and before PostgreSQL's text leaves the service. A passage that fails is dropped, and
  the answer doesn't say it existed. When permissions cannot be checked, nothing is returned (SRS 7.7).
- **Classification decides egress, not reading:** it limits what goes to the embedding provider (ADR 0011), and in
  Phase 8 what goes to the model. It does not grant or deny reading inside the platform.
- **Only what was indexed:** a held, failed or not-indexed document is never searched.

**Which revision is current:**

- **A revision is superseded** when a newer revision of the same document is indexed, or when another indexed
  document says it supersedes it.
- **Search returns current revisions only,** unless the caller asks for superseded ones too. Each result says whether
  it is current and, if not, what replaced it.
- **A citation stays valid:** its ID, `kn:<document>@<revision>#<passage>`, names one passage of one revision, which
  never changes. Reading it gives that passage as it was, marked superseded when it is.
- **Replacing a revision announces it:** indexing a revision that replaces another publishes `evidence.changed`, so
  the Investigation Service can mark recommendations that cite the old one stale (SRS 8.5, M8).
- **Re-indexing keeps provenance:** a revision is embedded again under the same point IDs, and its passages and
  citations stay as they were. Only the embedding model and version it records change.

## Consequences

- A user scoped to one wind farm searches within that farm: the request names it, or the search is refused.
- A query's text goes to the embedding provider to be embedded, as passages do. It is checked for credentials first,
  and logged by its length only.
- Withdrawing a document without replacing it is not yet possible; it is added when a screen needs it (Phase 11).
