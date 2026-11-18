# ADR 0011: OpenAI text-embedding-3-large, through the embedding adapter

**Status:** Accepted, 2026-11-16. This confirms the embedding provider vendor check (SRS 13.4, LLM-011).

## Context

The SRS fixes the starting embedding provider as OpenAI `text-embedding-3-large`, behind a provider adapter, with
each Qdrant point recording its model and version (LLM-011). It leaves the vendor check for Phase 7: confirm the
provider's terms and what may be sent to it. Embedding is the only point where document text leaves the platform
during ingestion. So what the provider keeps, and for how long, decides what a tenant may send.

What OpenAI states, checked on 2026-11-16 against its [data controls](https://developers.openai.com/api/docs/guides/your-data),
[model page](https://developers.openai.com/api/docs/models/text-embedding-3-large) and
[data residency announcement](https://openai.com/index/introducing-data-residency-in-europe/):

| | `text-embedding-3-large` |
| --- | --- |
| Training | API data is not used to train models |
| Retention | Up to 30 days in abuse-monitoring logs. Zero Data Retention removes this, but needs OpenAI's approval |
| Price | $0.13 per million input tokens |
| Vectors | 3,072 dimensions by default; shorter on request |
| Input | Up to 8,191 tokens per input |
| Data residency | Regions for new API projects, including Europe, the UAE and India. Not Saudi Arabia, and the embeddings endpoint's eligibility should be confirmed per region |

## Decision

- **Provider:** OpenAI `text-embedding-3-large` at its full 3,072 dimensions, behind an `Embedder` interface. The
  adapter is `openai-embeddings-1`, and every Qdrant point records the provider, the model the provider reports, the
  adapter's version and the dimensions.
- **One collection per model and size,** such as `knowledge_text_embedding_3_large_3072`. A new model is a new
  collection and a re-index (M7.3), never a mixture of vectors from two models.
- **What may go out is the tenant's decision.** Each tenant sets the highest classification sent to the provider.
  The default is `confidential`, so `restricted` documents stay inside the platform. A document above the allowance is
  kept, but not indexed. Raising the allowance needs `knowledge.manage` and a second factor, and is audited.
- **The demo tenants allow `restricted`.** They hold only synthetic documents, and their site procedures must be
  searchable to test tenant isolation.
- **Nothing else goes out:**
  - An upload holding a credential is refused before anything is kept (SRS 9.6).
  - A document that addresses instructions to an AI is held until a second person releases it (LLM-008).
  - Only passage text goes to the provider, with its document's title and section. No tenant, user or case
    identifier is sent.
- **The key** is `OPENAI_API_KEY` in `.env`. `make secrets` turns it into the Secret `windops-openai-api-key`, in the
  application namespace only, mounted as a file and read at each request. It is never a setting, an environment
  variable or a log field. Without it, the service runs and indexing waits.
- **Requests are logged by their size,** never their text (EGR-004). TLS is always verified. A 429, a 5xx, a timeout or
  no connection counts against the provider's breaker, and indexing backs off and tries again. Any other refusal fails
  that document, with the reason.

## Consequences

- **Abuse-monitoring retention:** OpenAI may keep embedded passages for up to 30 days. A production tenant with
  confidential material should ask OpenAI for Zero Data Retention, or keep its allowance at `internal`.
- **Data residency:** a deployment for Saudi tenants cannot keep embedding in the Kingdom with this provider.
  M15.10, the PDPL and data-residency review, decides whether it needs another provider behind the same adapter, such
  as Azure OpenAI in a region it accepts, or a model hosted on the platform.
- **Changing provider** is a new adapter and a re-index, with no change to the contract or the stored passages.
- **Cost** is small: the synthetic document set is about 25,000 tokens, under a cent to embed.
