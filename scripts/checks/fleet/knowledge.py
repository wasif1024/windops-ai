#!/usr/bin/env python3
"""Prove knowledge ingestion on the cluster, with real embeddings in Qdrant (M7.2).

Run `make secrets` with OPENAI_API_KEY in .env, then `make migrate`, the service and
`make knowledge-seed`. Calls are signed as Web BFF signs them, and every decision comes from the
real Authorization Service. The check proves:

- the demo seed: each demo tenant's documents, from the synthetic set's manifest, are indexed
  with OpenAI's text-embedding-3-large, a Qdrant point for each passage, each recording its model
  and version (LLM-011);
- in a tenant of its own, `knowledge-check`: an upload as a Tenant Administrator with a second
  factor keeps the document's own metadata, and is indexed; a password alone, or no
  knowledge.manage, is refused; so are a document holding a credential, and one that is not text;
- a document addressed to an AI is held, its uploader cannot release it, and another person can;
- a restricted document, above the tenant's default allowance, is kept but never indexed;
- another tenant cannot find the check's documents, and PostgreSQL refuses to change a kept
  document or passage, and hides them from another tenant.

What it made in its tenant, in PostgreSQL and Qdrant, is removed at the end.
"""

import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import grpc
import httpx
from windops.knowledge.v1 import knowledge_pb2 as pb
from windops.knowledge.v1 import knowledge_pb2_grpc
from windops_auth import LEVEL_PASSWORD
from windops_events import tls_context
from windops_simulator import documents

from scripts.checks.fleet.asset import Caller, bootstrap, check, kubectl, signing_key
from scripts.lib.tls_client import authority_file, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.provision_postgres import read_passwords

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-knowledge.windops-application.svc.cluster.local"
DATABASE = "windops_knowledge"
TENANT, DEMO, OTHER = "knowledge-check", "north-sea", "atlas-wind"
ADMIN, ENGINEER = "knowledge-check-admin", "knowledge-check-engineer"
OPERATOR, READER, STRANGER = ("knowledge-check-operator", "knowledge-check-reader",
                              "knowledge-check-stranger")  # fmt: skip
RUN = secrets.token_hex(3).upper()
COLLECTION = "knowledge_text_embedding_3_large_3072"
QDRANT_KEY = Path(__file__).resolve().parents[3] / "infrastructure" / "minikube" / \
    "secrets.local.qdrant.env"  # fmt: skip
# The provider answers in seconds; this allows for its backoff after one failure.
INDEX_SECONDS = 150
SET = {d.path: d for d in documents.load()}

CHECK_DOCUMENT = f"""# Knowledge Check Procedure {RUN}

| Field | Value |
| --- | --- |
| Document | KC-PRC-{RUN} |
| Revision | 1 |
| Type | procedure |
| Published | 2026-11-16 |
| Issued by | WindOps knowledge check (synthetic) |
| Applies to | WS-8.0-167 |
| Classification | internal |

> **Synthetic document.** Written by the knowledge check, and removed when it ends.

## 1. Purpose

Check the yaw drive's brake pads at every six-month visit, and replace any thinner than 4 mm.

## 2. Records

Record each pad's thickness against the turbine's case.
"""


def sql(statement: str, tenant: str = "") -> subprocess.CompletedProcess[str]:
    """As the Knowledge Service's own login, in one tenant, or across them all."""
    setting = (f"SELECT set_config('windops.tenant_id', '{tenant}', false); " if tenant
               else "SELECT set_config('windops.all_tenants', 'on', false); ")  # fmt: skip
    return kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={setting}{statement}",
        namespace=DATA,
    )  # fmt: skip


def value(statement: str, tenant: str = "") -> str:
    result = sql(statement, tenant)
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def qdrant_client(port: int) -> httpx.Client:
    key = read_passwords(QDRANT_KEY)["api-key"]
    return httpx.Client(base_url=f"https://127.0.0.1:{port}", headers={"api-key": key},
                        timeout=30, verify=tls_context(authority_file()))  # fmt: skip


def points(qdrant: httpx.Client, tenant: str, document_id: str) -> list[dict[str, Any]]:
    found = qdrant.post(f"/collections/{COLLECTION}/points/scroll", json={
        "limit": 500, "with_payload": True, "with_vector": False,
        "filter": {"must": [{"key": "tenant_id", "match": {"value": tenant}},
                            {"key": "document_id", "match": {"value": document_id}}]},
    })  # fmt: skip
    if found.status_code == 404:
        return []
    found.raise_for_status()
    result: list[dict[str, Any]] = found.json()["result"]["points"]
    return result


def upload_of(text: str, filename: str, content_type: str = "text/markdown",
              key: str = "") -> pb.UploadDocumentRequest:  # fmt: skip
    return pb.UploadDocumentRequest(filename=filename, content_type=content_type,
                                    content=text.encode(), source="knowledge-check",
                                    idempotency_key=key)  # fmt: skip


def get(knowledge: Caller, document_id: str, user: str, tenant: str) -> Any:
    return knowledge.call("GetDocument", pb.GetDocumentRequest(document_id=document_id), user,
                          tenant).document  # fmt: skip


def wait_indexed(knowledge: Caller, document_id: str, user: str, tenant: str) -> Any:
    deadline = time.monotonic() + INDEX_SECONDS
    while True:
        found = get(knowledge, document_id, user, tenant)
        if found.status != "pending" or time.monotonic() > deadline:
            return found
        time.sleep(2)


def is_indexed(found: Any, qdrant: httpx.Client, tenant: str) -> bool:
    kept = points(qdrant, tenant, found.metadata.document_id)
    kept = [p for p in kept if p["payload"]["revision"] == found.metadata.revision]
    return (
        found.status == "indexed" and found.embedding_provider == "openai"
        and found.embedding_model.startswith("text-embedding-3-large")
        and len(kept) == found.passages > 0
        and all(p["payload"]["embedding_model"] == found.embedding_model
                and p["payload"]["embedding_version"] == found.embedding_version
                and p["payload"]["dimensions"] == 3072 and "text" not in p["payload"]
                for p in kept)
    )  # fmt: skip


def check_seed(knowledge: Caller, qdrant: httpx.Client) -> None:
    wanted = {d.document_id for d in documents.current(tuple(SET.values()))
              if d.purpose == "demo" and DEMO in d.tenants}  # fmt: skip
    listed = knowledge.call("ListDocuments", pb.ListDocumentsRequest(page_size=200), READER, DEMO)
    newest = {d.metadata.document_id: d for d in listed.documents}
    check(set(newest) == wanted, f"{DEMO} has its {len(wanted)} demo documents, from the manifest")
    for document_id in sorted(wanted):
        found = wait_indexed(knowledge, document_id, READER, DEMO)
        check(is_indexed(found, qdrant, DEMO),
              f"{document_id} revision {found.metadata.revision} is indexed: {found.passages} "
              f"passages, {found.embedding_model}, {found.embedding_version}")  # fmt: skip


def check_upload(knowledge: Caller, qdrant: httpx.Client) -> str:
    document_id = f"KC-PRC-{RUN}"
    request = upload_of(CHECK_DOCUMENT, f"kc-prc-{RUN.lower()}.md", key=f"knowledge-check-{RUN}")
    for user, kw, who in ((ADMIN, {"level": LEVEL_PASSWORD}, "a password alone"),
                          (OPERATOR, {}, "no knowledge.manage")):  # fmt: skip
        check(knowledge.code("UploadDocument", request, user, TENANT, **kw) ==
              grpc.StatusCode.PERMISSION_DENIED, f"An upload with {who} is refused")  # fmt: skip
    kept = knowledge.call("UploadDocument", request, ADMIN, TENANT).document
    again = knowledge.call("UploadDocument", request, ADMIN, TENANT).document
    m = kept.metadata
    check(
        (m.document_id, m.revision, m.type, m.classification, list(m.applies_to)) ==
        (document_id, 1, "procedure", "internal", ["WS-8.0-167"])
        and "classification" in kept.metadata_from_document and again == kept,
        f"An upload keeps the metadata its own header states, and a retry returns it: "
        f"{kept.status}, {kept.passages} passages",
    )  # fmt: skip
    found = wait_indexed(knowledge, document_id, READER, TENANT)
    check(is_indexed(found, qdrant, TENANT),
          f"It is indexed with a point for each passage, recording "
          f"{found.embedding_model} and {found.embedding_version}")  # fmt: skip
    with_secret = upload_of(CHECK_DOCUMENT.replace(RUN, RUN + "S") + "\npassword: hunter2!\n",
                            "kc-secret.md")  # fmt: skip
    check(knowledge.code("UploadDocument", with_secret, ADMIN, TENANT) ==
          grpc.StatusCode.INVALID_ARGUMENT, "A document holding a password is refused")  # fmt: skip
    binary = pb.UploadDocumentRequest(filename="manual.pdf", content_type="application/pdf",
                                      content=b"%PDF-1.7\x00",
                                      source="knowledge-check")  # fmt: skip
    check(knowledge.code("UploadDocument", binary, ADMIN, TENANT) ==
          grpc.StatusCode.INVALID_ARGUMENT, "A document that is not text is refused")  # fmt: skip
    check(knowledge.code("GetDocument", pb.GetDocumentRequest(document_id=document_id), STRANGER,
                         OTHER) == grpc.StatusCode.NOT_FOUND,
          "Another tenant cannot find it")  # fmt: skip
    return document_id


def check_held(knowledge: Caller, qdrant: httpx.Client) -> None:
    injected = SET["evaluation/adv-sb-inj-001-r1.md"]
    held = knowledge.call("UploadDocument", upload_of(injected.text(), "adv-sb-inj-001-r1.md"),
                          ADMIN, TENANT).document  # fmt: skip
    check(held.status == "held" and len(held.screening) >= 3
          and not points(qdrant, TENANT, injected.document_id),
          f"A document addressed to an AI is held, unindexed: "
          f"{len(held.screening)} findings")  # fmt: skip
    release = pb.ReleaseDocumentRequest(document_id=injected.document_id, revision=1,
                                        reason="Knowledge check: test material")  # fmt: skip
    check(knowledge.code("ReleaseDocument", release, ADMIN, TENANT) ==
          grpc.StatusCode.FAILED_PRECONDITION, "Its uploader cannot release it")  # fmt: skip
    released = knowledge.call("ReleaseDocument", release, ENGINEER, TENANT).document
    found = wait_indexed(knowledge, injected.document_id, READER, TENANT)
    check(released.released_by == ENGINEER and is_indexed(found, qdrant, TENANT),
          "Another person releases it, and it is indexed")  # fmt: skip


def check_allowance(knowledge: Caller, qdrant: httpx.Client) -> None:
    site = SET["site/ns-site-off-001-r2.md"]
    settings = knowledge.call("GetIngestionSettings", pb.GetIngestionSettingsRequest(), READER,
                              TENANT).settings  # fmt: skip
    kept = knowledge.call("UploadDocument", upload_of(site.text(), "ns-site-off-001-r2.md"),
                          ADMIN, TENANT).document  # fmt: skip
    time.sleep(5)
    check(settings.embedding_max_classification == "confidential"
          and kept.status == "not_indexed"
          and get(knowledge, site.document_id, READER, TENANT).status == "not_indexed"
          and not points(qdrant, TENANT, site.document_id),
          "A restricted document, above the default allowance, is kept but never sent")  # fmt: skip


def check_database(document_id: str) -> None:
    for statement, says in (
        (f"UPDATE documents SET content = 'changed' WHERE document_id = '{document_id}'",  # noqa: S608  # The ID is the check's own.
         "never changes"),
        (f"DELETE FROM documents WHERE document_id = '{document_id}'", "never deleted"),  # noqa: S608
        (f"UPDATE passages SET text = 'changed' WHERE document_id = '{document_id}'",  # noqa: S608
         "never changes"),
    ):  # fmt: skip
        refused = sql(statement, TENANT)
        check(refused.returncode != 0 and says in refused.stderr,
              f"PostgreSQL refuses: {statement.split(' WHERE')[0]}")  # fmt: skip
    hidden = value(f"SELECT count(*) FROM documents WHERE document_id = '{document_id}'", OTHER)  # noqa: S608
    check(hidden == "0", "Row-level security hides it from another tenant")


def clean_up() -> None:
    statements = [
        "ALTER TABLE passages DISABLE TRIGGER passages_never_change",
        "ALTER TABLE documents DISABLE TRIGGER documents_keep_content",
        "DELETE FROM passages WHERE tenant_id = 'knowledge-check'",
        "DELETE FROM documents WHERE tenant_id = 'knowledge-check'",
        "DELETE FROM index_queue WHERE tenant_id = 'knowledge-check'",
        "DELETE FROM ingestion_settings WHERE tenant_id = 'knowledge-check'",
        "DELETE FROM idempotency_keys WHERE tenant_id = 'knowledge-check'",
        "ALTER TABLE passages ENABLE TRIGGER passages_never_change",
        "ALTER TABLE documents ENABLE TRIGGER documents_keep_content",
        "SELECT 1",
    ]
    value("; ".join(statements))
    with (port_forward("pod/windops-qdrant-0", DATA, 6333) as port,
          qdrant_client(port) as qdrant):  # fmt: skip
        removed = qdrant.post(f"/collections/{COLLECTION}/points/delete?wait=true", json={
            "filter": {"must": [{"key": "tenant_id", "match": {"value": TENANT}}]},
        })  # fmt: skip
        if removed.status_code not in (200, 404):
            raise RuntimeError(f"Could not remove the check's points: {removed.status_code}")


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        for tenant, user, role in (
            (TENANT, ADMIN, "tenant-administrator"), (TENANT, ENGINEER, "reliability-engineer"),
            (TENANT, OPERATOR, "control-room-operator"), (TENANT, READER, "reliability-engineer"),
            (DEMO, READER, "reliability-engineer"), (OTHER, STRANGER, "reliability-engineer"),
        ):  # fmt: skip
            bootstrap(tenant, user, role)
        with (
            port_forward("deployment/windops-knowledge", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
            port_forward("pod/windops-qdrant-0", DATA, 6333) as qdrant_port,
            qdrant_client(qdrant_port) as qdrant,
        ):
            knowledge = Caller(knowledge_pb2_grpc.KnowledgeServiceStub(channel), key)
            check_seed(knowledge, qdrant)
            document_id = check_upload(knowledge, qdrant)
            check_held(knowledge, qdrant)
            check_allowance(knowledge, qdrant)
        check_database(document_id)
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError,
            httpx.HTTPError) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Knowledge check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
