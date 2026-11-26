"""The knowledge library through real HTTP requests, with a fake Knowledge Service (M11.5)."""

import grpc
from test_admin import FakeService, stamp
from test_web_bff import World, auth, token, with_bff
from windops.knowledge.v1 import knowledge_pb2 as kn
from windops_auth import STEP_UP_MESSAGE
from windops_web_bff.api import LONG_CALLS, PREFIX

METADATA = kn.DocumentMetadata(
    document_id="WS-PRC-GBX-012", revision=3, title="Gearbox oil inspection", type="procedure",
    published="2026-03-01", issued_by="WindSim", applies_to=["WS-8.0-167"],
    classification="internal", supersedes_document_id="WS-PRC-GBX-012", supersedes_revision=2,
)  # fmt: skip
DOCUMENT = kn.DocumentVersion(
    metadata=METADATA, source="synthetic-documents-1", filename="ws-prc-gbx-012-r3.md",
    content_type="text/markdown", size_bytes=4096, content_hash="sha256:ab", status="held",
    status_reason="It holds text addressed to an AI", passages=6, ingested_by="u-admin",
    ingested_at=stamp(1_767_225_600),
    screening=[kn.ScreeningFinding(kind="instructions_to_ai", description="Addresses an AI",
                                   line=12)],
)  # fmt: skip
PASSAGE = kn.Passage(
    citation_id="kn:WS-PRC-GBX-012@3#0", document=METADATA, source="synthetic-documents-1",
    section="Gearbox oil inspection > Before you start", text="Lock the rotor.", first_line=3,
    last_line=5, content_hash="sha256:cd", current=True, embedding_model="text-embedding-3-large",
)  # fmt: skip


def knowledge_service() -> FakeService:
    return FakeService({
        "ListDocuments": kn.ListDocumentsResponse(documents=[DOCUMENT], next_page_token="t2"),
        "GetDocument": kn.GetDocumentResponse(document=DOCUMENT, revisions=[2, 3]),
        "ListPassages": kn.ListPassagesResponse(document=DOCUMENT, passages=[PASSAGE]),
        "SearchPassages": kn.SearchPassagesResponse(
            passages=[PASSAGE], embedding_model="text-embedding-3-large",
            embedding_version="adapter-1"),
        "GetPassage": kn.GetPassageResponse(passage=PASSAGE),
        "UploadDocument": kn.UploadDocumentResponse(document=DOCUMENT),
        "ReleaseDocument": kn.ReleaseDocumentResponse(document=DOCUMENT),
        "ReindexDocuments": kn.ReindexDocumentsResponse(queued=4),
        "GetIngestionSettings": kn.GetIngestionSettingsResponse(
            settings=kn.IngestionSettings(embedding_max_classification="confidential")),
        "SetIngestionSettings": kn.SetIngestionSettingsResponse(
            settings=kn.IngestionSettings(embedding_max_classification="internal",
                                          updated_by="u-1", updated_at=stamp(1_767_225_600))),
    })  # fmt: skip


def test_the_library_is_browsed_read_and_searched_where_the_user_reads() -> None:
    knowledge = knowledge_service()

    async def scenario(world: World) -> None:
        headers = auth(token())
        page = (await world.client.get(
            f"{PREFIX}/knowledge/documents?wind_farm_id=dogger-east&status=held",
            headers=headers)).json()  # fmt: skip
        assert page["next_page_token"] == "t2"
        [listed] = page["documents"]
        assert listed["metadata"]["applies_to"] == ["WS-8.0-167"]
        assert listed["screening"] == [
            {"kind": "instructions_to_ai", "description": "Addresses an AI", "line": 12}
        ]
        sent = knowledge.calls[-1].request
        assert (sent.wind_farm_id, sent.status, sent.page_size) == ("dogger-east", "held", 50)
        one = (await world.client.get(f"{PREFIX}/knowledge/documents/WS-PRC-GBX-012?revision=2",
                                      headers=headers)).json()  # fmt: skip
        assert one["revisions"] == [2, 3] and knowledge.calls[-1].request.revision == 2
        text = (await world.client.get(f"{PREFIX}/knowledge/documents/WS-PRC-GBX-012/passages",
                                       headers=headers)).json()  # fmt: skip
        assert text["passages"][0]["citation_id"] == "kn:WS-PRC-GBX-012@3#0"
        found = (await world.client.get(
            f"{PREFIX}/knowledge/search?q=oil+sample&types=procedure&types=manual&limit=5",
            headers=headers)).json()  # fmt: skip
        assert found["embedding_version"] == "adapter-1"
        asked = knowledge.calls[-1].request
        assert (asked.query, list(asked.types), asked.limit) == (
            "oil sample",
            ["procedure", "manual"],
            5,
        )
        cited = await world.client.get(
            f"{PREFIX}/knowledge/passage", params={"citation_id": "kn:WS-PRC-GBX-012@3#0"},
            headers=headers)  # fmt: skip
        assert cited.json()["section"].startswith("Gearbox oil inspection")
        for bad in ("/knowledge/documents/not-an-id", "/knowledge/documents?wind_farm_id=A B",
                    "/knowledge/passage?citation_id=WS-1", "/knowledge/search?q=",
                    "/knowledge/documents?status=lost"):  # fmt: skip
            assert (await world.client.get(PREFIX + bad, headers=headers)).status_code == 422, bad
        assert all(c.user_id == "u-1" for c in knowledge.calls), "as the signed-in user"

    with_bff(scenario, services={"knowledge": knowledge})


def test_changes_are_sent_once_as_the_person_and_a_step_up_is_asked_for() -> None:
    knowledge = knowledge_service()

    async def scenario(world: World) -> None:
        def change(n: int) -> dict[str, str]:
            return {**auth(token()), "Idempotency-Key": f"k-{n}"}

        made = await world.client.post(f"{PREFIX}/knowledge/documents", headers=change(1), json={
            "filename": "ws-prc-gbx-012-r3.md", "content_type": "text/markdown",
            "content": "# Gearbox oil inspection\n", "source": "Uploaded by hand"})  # fmt: skip
        assert made.status_code == 201
        sent = knowledge.calls[-1]
        assert (sent.request.content, sent.request.idempotency_key, sent.retry) == (
            b"# Gearbox oil inspection\n", "k-1", False)  # fmt: skip
        refused = await world.client.post(f"{PREFIX}/knowledge/documents", headers=change(2), json={
            "filename": "manual.pdf", "content_type": "application/pdf", "content": "x",
            "source": "s"})  # fmt: skip
        assert refused.status_code == 422
        released = await world.client.post(
            f"{PREFIX}/knowledge/documents/WS-PRC-GBX-012/revisions/3/release",
            headers=change(3), json={"reason": "Reviewed: the text is test material"})  # fmt: skip
        assert released.status_code == 200 and knowledge.calls[-1].request.revision == 3
        queued = await world.client.post(f"{PREFIX}/knowledge/reindex", headers=change(4),
                                         json={"reason": "New embedding model"})  # fmt: skip
        assert queued.json() == {"queued": 4} and knowledge.calls[-1].request.document_id == ""
        got = await world.client.get(f"{PREFIX}/knowledge/settings", headers=auth(token()))
        assert got.json()["embedding_max_classification"] == "confidential"
        put = await world.client.put(f"{PREFIX}/knowledge/settings", headers=change(5), json={
            "embedding_max_classification": "internal", "reason": "Tighter"})  # fmt: skip
        assert put.json()["updated_by"] == "u-1"
        assert all(not c.retry for c in knowledge.calls
                   if c.method not in ("GetIngestionSettings",))  # fmt: skip
        knowledge.error = grpc.aio.AioRpcError(
            grpc.StatusCode.PERMISSION_DENIED, grpc.aio.Metadata(), grpc.aio.Metadata(),
            details=STEP_UP_MESSAGE)  # fmt: skip
        stepped = await world.client.post(f"{PREFIX}/knowledge/reindex", headers=change(6),
                                          json={"reason": "Again"})  # fmt: skip
        assert stepped.status_code == 401 and stepped.json()["code"] == "step_up_required"

    with_bff(scenario, services={"knowledge": knowledge})


def test_a_search_waits_for_its_question_to_be_embedded() -> None:
    assert "SearchPassages" in LONG_CALLS
