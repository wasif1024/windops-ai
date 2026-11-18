#!/usr/bin/env python3
"""Prove retrieval on the cluster, with real embeddings: citations, tenants, permissions, current
revisions and re-indexing (M7.3, FR-KNW-003 to 005, DAT-006, ADR 0012).

Run `make knowledge-seed` first, as `make knowledge-check` does. The check proves:

- on the demo documents, with OpenAI's embeddings, a question finds the procedure that answers it,
  as cited passages of its current revision; the revision it replaced comes back only when asked,
  marked superseded, and its citation still reads;
- each tenant searches only its own documents, whatever it asks;
- in a tenant of its own, `knowledge-check`: a user scoped to one wind farm searches there or not
  at all, and never sees another farm's site document, even by its citation;
- a new revision replaces the old in search, and announces it on evidence.changed;
- re-indexing keeps every citation, and records the new embedding;
- a query holding a credential is never sent to be embedded.

What it made in its tenant is removed at the end, as the knowledge check removes its own.
"""

import json
import secrets
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
from windops.authorization.v1 import authorization_pb2 as authz
from windops.authorization.v1 import authorization_pb2_grpc
from windops.knowledge.v1 import knowledge_pb2 as pb
from windops.knowledge.v1 import knowledge_pb2_grpc

from scripts.checks.fleet import knowledge as base
from scripts.checks.fleet.asset import Caller, bootstrap, check, signing_key
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, SYSTEM, DATA = "windops-application", "windops-system", "windops-data"
AUTHORIZATION_HOST = "windops-authorization.windops-system.svc.cluster.local"
TENANT, DEMO, OTHER = base.TENANT, base.DEMO, base.OTHER
ADMIN, READER = base.ADMIN, base.READER
RUN = secrets.token_hex(3)
SCOPED = f"knowledge-check-scoped-{RUN}"
FARM_A, FARM_B = "check-farm-a", "check-farm-b"
SITE_ID, PROCEDURE_ID = f"KC-SITE-{RUN.upper()}", f"KC-YAW-{RUN.upper()}"


def document(document_id: str, revision: int, applies_to: str, body: str,
             supersedes: str = "") -> str:  # fmt: skip
    supersedes_row = f"| Supersedes | {supersedes} |\n" if supersedes else ""
    return f"""# Retrieval Check {document_id}

| Field | Value |
| --- | --- |
| Document | {document_id} |
| Revision | {revision} |
| Type | procedure |
| Published | 2026-11-1{revision} |
| Issued by | WindOps retrieval check (synthetic) |
| Applies to | {applies_to} |
| Classification | internal |
{supersedes_row}
> **Synthetic document.** Written by the retrieval check, and removed when it ends.

## 1. Procedure

{body}
"""


def search(knowledge: Caller, query: str, user: str = READER, tenant: str = DEMO,
           **fields: Any) -> list[Any]:  # fmt: skip
    request = pb.SearchPassagesRequest(query=query, **fields)
    return list(knowledge.call("SearchPassages", request, user, tenant).passages)


def upload(knowledge: Caller, text: str, filename: str) -> Any:
    request = base.upload_of(text, filename)
    return knowledge.call("UploadDocument", request, ADMIN, TENANT).document


def indexed(knowledge: Caller, document_id: str, revision: int = 0) -> Any:
    deadline = time.monotonic() + base.INDEX_SECONDS
    while True:
        found = knowledge.call("GetDocument", pb.GetDocumentRequest(
            document_id=document_id, revision=revision), READER, TENANT).document  # fmt: skip
        if found.status == "indexed" or time.monotonic() > deadline:
            check(found.status == "indexed", f"{document_id}@{found.metadata.revision} is indexed")
            return found
        time.sleep(2)


def check_demo_answers(knowledge: Caller) -> None:
    question = "The gearbox oil temperature is critical. May the turbine run on at reduced power?"
    found = search(knowledge, question, limit=3)
    top = found[0]
    check(top.document.document_id == "WS-PRC-GBX-012" and top.document.revision == 3
          and top.current and "Running on at reduced power is not allowed" in top.text,
          f"A question finds the current procedure that answers it: {top.citation_id}, "
          f"score {top.score:.3f}")  # fmt: skip
    check(
        all(p.document.revision == 3 for p in found if p.document.document_id == "WS-PRC-GBX-012"),
        "The revision that allowed running on is not returned",
    )
    old = [p for p in search(knowledge, question, limit=10, include_superseded=True)
           if p.document.document_id == "WS-PRC-GBX-012" and p.document.revision == 2]  # fmt: skip
    check(bool(old) and all(not p.current and p.superseded_by == "WS-PRC-GBX-012@3"
                            for p in old),
          "Asked for, the old revision comes back marked superseded by revision 3")  # fmt: skip
    read = knowledge.call("GetPassage", pb.GetPassageRequest(citation_id=old[0].citation_id),
                          READER, DEMO).passage  # fmt: skip
    check(read.text == old[0].text and not read.current,
          f"Its citation, {old[0].citation_id}, still reads as it was")  # fmt: skip
    climb = search(knowledge, "What is the wind speed limit for climbing the tower?", limit=1)[0]
    check(climb.document.document_id == "WS-SAF-001" and "12 m/s" in climb.text,
          f"The climbing limit is found in the safety procedure: {climb.citation_id}")  # fmt: skip
    transfer = "What wave height stops a crew transfer to the turbine?"
    ours = search(knowledge, transfer, limit=1)[0]
    theirs = search(knowledge, transfer, user=base.STRANGER, tenant=OTHER, limit=10)
    check(ours.document.document_id == "NS-SITE-OFF-001" and "1.5 m" in ours.text
          and "NS-SITE-OFF-001" not in {p.document.document_id for p in theirs},
          f"{DEMO} finds its own transfer limits; {OTHER} never sees them")  # fmt: skip


def check_scoped(knowledge: Caller, authorization: Caller) -> None:
    upload(
        knowledge,
        document(
            SITE_ID,
            1,
            FARM_A,
            "Torque the check-farm-a substation crane pad anchors to 450 Nm before a heavy lift.",
        ),
        "kc-site.md",
    )
    upload(
        knowledge,
        document(
            PROCEDURE_ID,
            1,
            "WS-8.0-167",
            "Replace yaw brake pads thinner than 4 mm, and record each pad's thickness.",
        ),
        "kc-yaw-1.md",
    )
    indexed(knowledge, SITE_ID)
    indexed(knowledge, PROCEDURE_ID)
    granted = authorization.call("GrantRole", authz.GrantRoleRequest(
        grantee_id=SCOPED, role="reliability-engineer", scope=authz.Scope(wind_farm_id=FARM_B),
        idempotency_key=f"retrieval-check-{RUN}"), ADMIN, TENANT)  # fmt: skip
    check(
        granted.grant.scope.wind_farm_id == FARM_B, f"A reliability engineer is scoped to {FARM_B}"
    )
    question = "How tight are the crane pad anchors, and when are yaw brake pads replaced?"
    check(knowledge.code("SearchPassages", pb.SearchPassagesRequest(query=question), SCOPED,
                         TENANT) == grpc.StatusCode.PERMISSION_DENIED,
          "Scoped to one farm, a search of the whole tenant is refused")  # fmt: skip
    theirs = {p.document.document_id for p in search(knowledge, question, user=SCOPED,
                                                     tenant=TENANT, wind_farm_id=FARM_B,
                                                     limit=10)}  # fmt: skip
    everyone = search(knowledge, question, tenant=TENANT, limit=10)
    check(
        PROCEDURE_ID in theirs
        and SITE_ID not in theirs
        and SITE_ID in {p.document.document_id for p in everyone},
        f"In {FARM_B}, the manufacturer's procedure is found, never {FARM_A}'s site document",
    )
    citation = next(p.citation_id for p in everyone if p.document.document_id == SITE_ID)
    check(knowledge.code("GetPassage", pb.GetPassageRequest(citation_id=citation,
                                                            wind_farm_id=FARM_B),
                         SCOPED, TENANT) == grpc.StatusCode.NOT_FOUND,
          "Nor by its citation")  # fmt: skip


def check_replacement(knowledge: Caller) -> None:
    before = indexed(knowledge, PROCEDURE_ID)
    upload(knowledge, document(PROCEDURE_ID, 2, "WS-8.0-167", "Replace yaw brake pads thinner "
                               "than 5 mm, and record each pad's thickness.",
                               supersedes=f"{PROCEDURE_ID} revision 1"), "kc-yaw-2.md")  # fmt: skip
    indexed(knowledge, PROCEDURE_ID, 2)
    found = [p for p in search(knowledge, "When are yaw brake pads replaced?", tenant=TENANT,
                               limit=10) if p.document.document_id == PROCEDURE_ID]  # fmt: skip
    check(all(p.document.revision == 2 for p in found) and any("5 mm" in p.text for p in found),
          f"Revision 2 replaces revision {before.metadata.revision} in search")  # fmt: skip
    announced = base.value(
        "SELECT count(*) FROM outbox_events WHERE subject = 'evidence.changed' AND tenant_id = "
        "'knowledge-check' AND published_at IS NOT NULL"
    )
    check(announced not in ("", "0"), "The replacement is announced on evidence.changed, and sent")


def check_reindex(knowledge: Caller) -> None:
    question = "When are yaw brake pads replaced?"
    before = {p.citation_id: p.text for p in search(knowledge, question, tenant=TENANT,
                                                    include_superseded=True, limit=20)}  # fmt: skip
    first = indexed(knowledge, PROCEDURE_ID, 2).indexed_at.ToDatetime()
    queued = knowledge.call("ReindexDocuments", pb.ReindexDocumentsRequest(
        document_id=PROCEDURE_ID, reason="Retrieval check"), ADMIN, TENANT).queued  # fmt: skip
    deadline = time.monotonic() + base.INDEX_SECONDS
    while time.monotonic() < deadline:
        found = indexed(knowledge, PROCEDURE_ID, 2)
        if found.indexed_at.ToDatetime() > first:
            break
        time.sleep(2)
    after = {p.citation_id: p.text for p in search(knowledge, question, tenant=TENANT,
                                                   include_superseded=True, limit=20)}  # fmt: skip
    check(queued == 2 and found.indexed_at.ToDatetime() > first and after == before,
          f"Re-indexing {queued} revisions keeps every citation: {found.embedding_model}, "
          f"{found.embedding_version}")  # fmt: skip


def check_refusals(knowledge: Caller) -> None:
    secret = pb.SearchPassagesRequest(query="password: " + "hunter2!")
    check(knowledge.code("SearchPassages", secret, READER, DEMO) ==
          grpc.StatusCode.INVALID_ARGUMENT, "A query holding a credential is not sent")  # fmt: skip


def main() -> int:
    try:
        base.clean_up()
        key, bundle = signing_key(), client_bundle()
        for tenant, user, role in ((TENANT, ADMIN, "tenant-administrator"),
                                   (TENANT, READER, "reliability-engineer"),
                                   (DEMO, READER, "reliability-engineer"),
                                   (OTHER, base.STRANGER, "reliability-engineer")):  # fmt: skip
            bootstrap(tenant, user, role)
        with (
            port_forward("deployment/windops-knowledge", APPLICATION, 50051) as port,
            blocking_channel(port, base.HOST, bundle) as channel,
            port_forward("deployment/windops-authorization", SYSTEM, 50051) as authz_port,
            blocking_channel(authz_port, AUTHORIZATION_HOST, bundle) as authz_channel,
        ):
            knowledge = Caller(knowledge_pb2_grpc.KnowledgeServiceStub(channel), key)
            authorization = Caller(authorization_pb2_grpc.AuthorizationServiceStub(authz_channel),
                                   key)  # fmt: skip
            check_demo_answers(knowledge)
            check_scoped(knowledge, authorization)
            check_replacement(knowledge)
            check_reindex(knowledge)
            check_refusals(knowledge)
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError,
            httpx.HTTPError, json.JSONDecodeError) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        base.clean_up()
    print("Retrieval check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
