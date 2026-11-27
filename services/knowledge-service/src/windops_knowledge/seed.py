"""Ingest the synthetic document set for the demo tenants and the evaluation tenant. Safe to
repeat.

Run inside the service's pod, with its own database login, by `make knowledge-seed`:

    python -m windops_knowledge.seed

Each document goes to the tenants its manifest names, through the same checks and ingestion an
upload takes, as the actor "seed". The evaluation material, such as the documents carrying a
prompt injection, names only the evaluation tenant, where the regression runs (M12.6), so it
never reaches a demo tenant. These tenants hold only synthetic documents, so the seed
allows each to send restricted ones to the embedding provider, which their site procedures are,
for retrieval and tenant isolation to be tested on them. A real tenant's administrator decides
that for themselves (ADR 0011).
"""

import asyncio
import sys
from datetime import UTC, datetime

from windops_migrations import create_service_engine, in_tenant
from windops_simulator import documents

from windops_knowledge import library, uploads

ACTOR = "seed"
CONTENT_TYPE = "text/markdown"
DEMO_ALLOWANCE = "restricted"


def source() -> str:
    return f"WindOps Synthetic document set {documents.set_version()}"


async def seed() -> tuple[int, int]:
    """Return how many revisions were added, and how many were already there."""
    engine = create_service_engine()
    added = kept = 0
    # Each document names its tenants: evaluation material names only the evaluation tenant.
    found = list(documents.load())
    tenants = sorted({t for d in found for t in d.tenants})
    try:
        for tenant in tenants:
            async with engine.begin() as connection:
                await in_tenant(connection, tenant)
                current = await library.allowance(connection, tenant)
                if current.embedding_max_classification != DEMO_ALLOWANCE:
                    await library.set_allowance(connection, tenant, "service", ACTOR,
                                                DEMO_ALLOWANCE, datetime.now(UTC))  # fmt: skip
            for document in (d for d in found if tenant in d.tenants):
                filename = document.path.rsplit("/", 1)[-1]
                upload = uploads.prepare(filename, CONTENT_TYPE, document.text().encode())
                # One transaction per document, so each is all there or not at all.
                async with engine.begin() as connection:
                    await in_tenant(connection, tenant)
                    _, new = await library.ingest(
                        connection, tenant, "service", ACTOR, upload, filename=filename,
                        content_type=CONTENT_TYPE, source=source(), now=datetime.now(UTC),
                    )  # fmt: skip
                added, kept = added + new, kept + (not new)
    finally:
        await engine.dispose()
    return added, kept


def main() -> int:
    added, kept = asyncio.run(seed())
    print(f"Added {added} document revisions; {kept} were already there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
