"""Retrieval: citable passages, the tenant filter, document permissions, current revisions and
re-indexing (FR-KNW-003 to 005, DAT-006, ADR 0012), with the same stand-ins as the API's tests."""

from typing import Any

import grpc
from sqlalchemy import select
from test_knowledge_service import (
    GEARBOX,
    INJECTED,
    OTHER,
    SET,
    SITE,
    World,
    as_user,
    audited,
    code_of,
    get,
    upload,
    with_service,
)
from windops.knowledge.v1 import knowledge_pb2 as pb
from windops_auth import LEVEL_PASSWORD
from windops_events import parse_event
from windops_knowledge import retrieval
from windops_knowledge.store import PASSAGES
from windops_outbox import OUTBOX_TABLE

GEARBOX_OLD = SET["oem/ws-prc-gbx-012-r2.md"]
ALARMS = SET["oem/ws-ref-alm-r1.md"]
SAFETY = SET["oem/ws-saf-001-r5.md"]
BEARING = SET["oem/ws-prc-brg-021-r1.md"]
QUESTION = "The gearbox oil temperature is critical: may the turbine run on at reduced power?"

DOGGER_ONLY = """# Dogger East Crane Pad Inspection

| Field | Value |
| --- | --- |
| Document | NS-SITE-PAD-001 |
| Revision | 1 |
| Type | procedure |
| Published | 2026-03-01 |
| Issued by | North Sea operations team (synthetic tenant) |
| Applies to | dogger-east |
| Classification | internal |

## 1. Inspection

Inspect the crane pads at the Dogger East substation before every heavy lift.
"""


async def search(world: World, query: str = QUESTION, user: str = "u-reader",
                 tenant: str = "north-sea", **fields: Any) -> Any:  # fmt: skip
    return await world.stub.SearchPassages(
        pb.SearchPassagesRequest(query=query, **fields),
        metadata=as_user(user, tenant=tenant),
        timeout=5,
    )


async def load(world: World, *documents: Any, tenant: str = "north-sea") -> None:
    for document in documents:
        await upload(world, document, tenant=tenant)
    await world.index()


async def upload_text(world: World, text: str, filename: str) -> None:
    await world.stub.UploadDocument(
        pb.UploadDocumentRequest(filename=filename, content_type="text/markdown",
                                 content=text.encode(), source="test"),
        metadata=as_user("u-admin"), timeout=5,
    )  # fmt: skip
    await world.index()


def test_a_search_returns_cited_passages_of_current_revisions_best_first() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX_OLD, GEARBOX, ALARMS, SAFETY, BEARING)
        found = await search(world, limit=5)
        passages = list(found.passages)
        # A stand-in embedding cannot rank by meaning; the cluster check ranks with the real one.
        assert len(passages) == 5 and "WS-PRC-GBX-012" in {p.document.document_id
                                                           for p in passages}  # fmt: skip
        assert [p.score for p in passages] == sorted((p.score for p in passages), reverse=True)
        # Revision 2 said it could run on; revision 3 withdrew that. Only 3 is current.
        assert all(p.document.revision == 3 for p in passages
                   if p.document.document_id == "WS-PRC-GBX-012")  # fmt: skip
        assert all(p.current and not p.superseded_by for p in passages)
        first = next(p for p in passages if p.document.document_id == "WS-PRC-GBX-012")
        assert first.citation_id == f"kn:WS-PRC-GBX-012@3#{first.citation_id.rsplit('#')[1]}"
        # The text is the document's own, where the citation says it is.
        assert GEARBOX.text()[first.start_offset : first.end_offset] == first.text
        assert first.section.startswith(GEARBOX.title)
        assert (first.embedding_model, found.embedding_model) == ("text-embedding-3-large",) * 2
        assert world.embedder.sent[-1] == QUESTION.strip()

    with_service(scenario)


def test_superseded_revisions_only_when_asked_and_then_marked() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX_OLD, GEARBOX)
        both = list((await search(world, include_superseded=True, limit=20)).passages)
        old = [p for p in both if p.document.revision == 2]
        assert old and all(not p.current and p.superseded_by == "WS-PRC-GBX-012@3" for p in old)
        # A citation of the old revision still reads, as it was, marked.
        read = await world.stub.GetPassage(pb.GetPassageRequest(citation_id=old[0].citation_id),
                                           metadata=as_user("u-reader"), timeout=5)  # fmt: skip
        assert read.passage.text == old[0].text and not read.passage.current
        assert read.passage.superseded_by == "WS-PRC-GBX-012@3" and read.passage.score == 0

    with_service(scenario)


def test_only_the_callers_tenant_is_searched_whatever_the_request() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX)
        await load(world, BEARING, tenant=OTHER)
        theirs = await search(world, tenant=OTHER, user="u-reader", limit=20)
        assert {p.document.document_id for p in theirs.passages} == {"WS-PRC-BRG-021"}
        assert world.qdrant.searched == [OTHER]
        ours = await search(world, limit=20)
        assert "WS-PRC-BRG-021" not in {p.document.document_id for p in ours.passages}

    with_service(scenario)


def test_a_document_is_returned_only_to_those_who_may_read_where_it_applies() -> None:
    async def scenario(world: World) -> None:
        # The site procedure is restricted: the tenant allows it to be embedded first.
        await world.stub.SetIngestionSettings(
            pb.SetIngestionSettingsRequest(embedding_max_classification="restricted",
                                           reason="Synthetic documents only"),
            metadata=as_user("u-admin"), timeout=5,
        )  # fmt: skip
        await load(world, GEARBOX, SITE)
        await upload_text(world, DOGGER_ONLY, "ns-site-pad-001.md")
        query = "inspect crane pads before a heavy lift, and offshore transfer limits"

        def ids(found: Any) -> set[str]:
            return {p.document.document_id for p in found.passages}

        everyone = ids(await search(world, query, limit=20))
        assert {"NS-SITE-PAD-001", "NS-SITE-OFF-001", "WS-PRC-GBX-012"} <= everyone
        # Scoped to one farm: the search is made there, or not at all.
        refused = search(world, query, user="u-hornsea")
        assert await code_of(refused) == grpc.StatusCode.PERMISSION_DENIED
        hornsea = ids(await search(world, query, user="u-hornsea", wind_farm_id="hornsea-west",
                                   limit=20))  # fmt: skip
        assert "NS-SITE-OFF-001" in hornsea and "WS-PRC-GBX-012" in hornsea
        assert "NS-SITE-PAD-001" not in hornsea, "it applies to Dogger East only"
        dogger = ids(await search(world, query, user="u-dogger", wind_farm_id="dogger-east",
                                  limit=20))  # fmt: skip
        assert "NS-SITE-PAD-001" in dogger
        # Reading its citation is no way round it.
        pad = next(p for p in (await search(world, query, limit=20)).passages
                   if p.document.document_id == "NS-SITE-PAD-001")  # fmt: skip
        read = world.stub.GetPassage(pb.GetPassageRequest(citation_id=pad.citation_id,
                                                          wind_farm_id="hornsea-west"),
                                     metadata=as_user("u-hornsea"), timeout=5)  # fmt: skip
        assert await code_of(read) == grpc.StatusCode.NOT_FOUND
        assert await code_of(search(world, query, user="u-nobody")) == \
            grpc.StatusCode.PERMISSION_DENIED  # fmt: skip

    with_service(scenario)


def test_what_was_not_indexed_or_has_changed_is_never_returned() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX, INJECTED)
        assert "ADV-SB-INJ-001" not in {
            p.document.document_id for p in (await search(world, limit=20)).passages
        }
        # A point whose text no longer matches PostgreSQL's is dropped.
        point = next(p for p in world.qdrant.points.values() if p.ordinal == 0)
        point.payload["content_hash"] = "0" * 64
        found = await search(world, limit=20)
        assert "kn:WS-PRC-GBX-012@3#0" not in {p.citation_id for p in found.passages}

    with_service(scenario)


def test_without_its_dependencies_a_search_returns_nothing() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX)
        for name in ("embedder", "qdrant", "authorizer"):
            if name == "embedder":
                from windops_knowledge import embeddings

                world.embedder.failure = embeddings.EmbeddingUnavailableError("down")
            elif name == "qdrant":
                world.embedder.failure, world.qdrant.down = None, True
            else:
                world.qdrant.down, world.authorizer.down = False, True
            assert await code_of(search(world)) == grpc.StatusCode.UNAVAILABLE, name

    with_service(scenario)


def test_a_query_holding_a_credential_is_not_sent() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX)
        sent = len(world.embedder.sent)
        for query, code in (("password: " + "hunter2!", grpc.StatusCode.INVALID_ARGUMENT),
                            ("", grpc.StatusCode.INVALID_ARGUMENT),
                            ("x" * 1001, grpc.StatusCode.INVALID_ARGUMENT)):  # fmt: skip
            assert await code_of(search(world, query)) == code
        assert len(world.embedder.sent) == sent
        assert await code_of(search(world, limit=21)) == grpc.StatusCode.INVALID_ARGUMENT
        assert await code_of(search(world, types=["memo"])) == grpc.StatusCode.INVALID_ARGUMENT

    with_service(scenario)


def test_filters_by_what_a_document_applies_to_and_its_type() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX, SET["oem/ws-om-8167-r4.md"], SET["oem/ws-sb-2025-07-r1.md"])
        bulletins = await search(world, types=["bulletin"], limit=20)
        assert {p.document.document_id for p in bulletins.passages} == {"WS-SB-2025-07"}
        manual = await search(world, applies_to="WS-8.0-167", types=["manual"], limit=20)
        assert {p.document.document_id for p in manual.passages} == {"WS-OM-8167"}

    with_service(scenario)


def test_bad_citations_are_not_found() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX)
        for citation in ("kn:WS-PRC-GBX-012@3#999", "kn:WS-PRC-GBX-012@9#0", "WS-PRC-GBX-012",
                         "kn:ws@1#1"):  # fmt: skip
            read = world.stub.GetPassage(pb.GetPassageRequest(citation_id=citation),
                                         metadata=as_user("u-reader"), timeout=5)  # fmt: skip
            assert await code_of(read) == grpc.StatusCode.NOT_FOUND, citation
        assert retrieval.parse_citation("kn:WS-PRC-GBX-012@3#4") == ("WS-PRC-GBX-012", 3, 4)

    with_service(scenario)


def test_re_indexing_keeps_every_citation_and_records_the_new_embedding() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX_OLD, GEARBOX)
        before = {p.citation_id: p.text for p in (await search(world, include_superseded=True,
                                                                limit=20)).passages}  # fmt: skip
        points = set(world.qdrant.points)

        def reindex(user: str = "u-admin", document_id: str = "WS-PRC-GBX-012",
                    **kw: Any) -> Any:  # fmt: skip
            return world.stub.ReindexDocuments(
                pb.ReindexDocumentsRequest(document_id=document_id, reason="A new adapter"),
                metadata=as_user(user, **kw), timeout=5,
            )  # fmt: skip

        assert await code_of(reindex("u-reader")) == grpc.StatusCode.PERMISSION_DENIED
        assert await code_of(reindex(level=LEVEL_PASSWORD)) == grpc.StatusCode.PERMISSION_DENIED
        assert await code_of(reindex(document_id="NO-SUCH-001")) == grpc.StatusCode.NOT_FOUND
        world.embedder.version = "openai-embeddings-2"
        assert (await reindex()).queued == 2
        # Still searchable while it waits, on its earlier vectors.
        assert (await get(world, "WS-PRC-GBX-012")).document.status == "indexed"
        assert await world.index() == 2
        after = {p.citation_id: p.text for p in (await search(world, include_superseded=True,
                                                               limit=20)).passages}  # fmt: skip
        assert after == before and set(world.qdrant.points) == points
        found = (await get(world, "WS-PRC-GBX-012")).document
        assert found.status == "indexed" and found.embedding_version == "openai-embeddings-2"
        assert all(p.payload["embedding_version"] == "openai-embeddings-2"
                   for p in world.qdrant.points.values())  # fmt: skip
        assert (await audited(world)).count("knowledge_document.reindexed") == 2
        async with world.engine.connect() as connection:
            kept = (await connection.execute(select(PASSAGES))).all()
        assert len(kept) == len(points), "the passages themselves are never cut again"

    with_service(scenario)


def test_a_replacing_revision_announces_the_revisions_it_replaces() -> None:
    async def scenario(world: World) -> None:
        await load(world, GEARBOX_OLD)
        await load(world, GEARBOX)
        o = OUTBOX_TABLE.c
        async with world.engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(o.subject, o.envelope).where(o.subject == "evidence.changed")
                )
            ).all()
        announced = [parse_event(r.subject, r.envelope).payload for r in rows]
        assert [(e.source_service, e.record_id, e.version) for e in announced] == [  # type: ignore[attr-defined]
            ("knowledge", "WS-PRC-GBX-012", "WS-PRC-GBX-012@3"),
        ]  # fmt: skip

    with_service(scenario)
