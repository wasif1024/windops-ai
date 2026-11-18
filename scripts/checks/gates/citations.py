#!/usr/bin/env python3
"""Prove the Phase 7 gate the way an agent will meet it: through the MCP servers.

SRS section 12: *results carry citations and provenance, and permissions are enforced before
return.* Real users sign in, the Identity Service exchanges their tokens for the Documentation,
Tavily and Weather servers, and every result is checked as it arrives:

- **Citations:** every passage the demo questions find is cited `kn:<document>@<revision>#<n>`,
  names the document and revision its citation does, is a revision in the synthetic set's manifest
  for that tenant, and holds exactly the text its SHA-256 hash says. Reading it by its citation
  gives the same text and hash.
- **Provenance:** a web search says what it sent and what it took out, and each source its
  address, domain, retrieval time and flags, each flag with its note; reading the search again
  gives the same sources. A forecast names its provider, licence, retrieval time, valid span and
  hash, and is fresh or says it is stale.
- **Permissions before return:** a user of another tenant never receives this tenant's documents,
  even by citation; a user scoped to one wind farm cannot search the whole tenant, or the other
  farm, or research it; a user with no role gets nothing from any server. Each refusal comes back
  with no content at all.

Two temporary users are made in the demo tenant, and removed with their grant at the end. Run
`make identity-seed`, `make knowledge-seed` and `make services` first. Needs local port 9443.
"""

import asyncio
import hashlib
import json
import re
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import grpc
import httpx
import jwt
from windops.authorization.v1 import authorization_pb2 as authz
from windops.authorization.v1 import authorization_pb2_grpc
from windops.identity.v1 import identity_pb2_grpc
from windops_mcp import unwrap

from scripts.checks.fleet.asset import Caller as SignedCaller
from scripts.checks.fleet.mcp_servers import Server, body, forwards, tls_files, tokens_for, wrapped
from scripts.checks.identity.identity import Realm, delete_user
from scripts.checks.identity.token_exchange import HOST, SYSTEM, Caller, check, signing_key
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import (
    ADMIN_SECRET_FILE,
    REALM,
    TENANTS,
    admin_client,
    demo_admin_email,
    demo_email,
    ensure_member,
    ensure_tenant,
    ensure_user,
    keycloak_admin,
    keycloak_public,
    read_demo_passwords,
    strong_password,
)
from scripts.setup.provision_postgres import read_passwords

DEMO, OTHER = "north-sea", "atlas-wind"
FARM, OTHER_FARM = "dogger-east", "hornsea-west"
TURBINE, OTHER_TURBINE = "dogger-east-t01", "hornsea-west-t02"
RUN = secrets.token_hex(3)
SCOPED_EMAIL = f"citation-check-scoped-{RUN}@windops-check.example"
NOBODY_EMAIL = f"citation-check-nobody-{RUN}@windops-check.example"
AUTHORIZATION_HOST = "windops-authorization.windops-system.svc.cluster.local"
MANIFEST = (
    Path(__file__).resolve().parents[3] / "simulator/src/windops_simulator/documents/manifest.json"
)
CITATION = re.compile(r"^kn:(?P<document>[A-Za-z0-9_.-]+)@(?P<revision>\d+)#(?P<passage>\d+)$")
QUESTIONS = (
    "What gearbox oil debris levels need an inspection?",
    "How are main bearing temperatures judged?",
    "What wind speed stops work at height in the nacelle?",
    "Which alarm codes come from the pitch system?",
    "How is a rotor blade inspected after a lightning strike?",
    "What must a crew do before transferring to an offshore turbine?",
)
FLAGS = {"stale", "undated", "low_confidence", "conflicting", "instructions_to_ai"}


def manifest(tenant: str) -> dict[tuple[str, int], dict[str, Any]]:
    documents = json.loads(MANIFEST.read_text())["documents"]
    return {
        (item["document_id"], item["revision"]): item
        for item in documents
        if tenant in item.get("tenants", [])
    }


def refused(found: dict[str, Any]) -> bool:
    """A refusal: an error and a message, and nothing else, so no content came back."""
    return found.get("error") == "refused" and set(found) <= {"error", "message"}


async def check_citations(server: Server, token: str) -> list[str]:
    """Every passage cited, from the manifest, and holding exactly its hashed text."""
    known = manifest(DEMO)
    passages: dict[str, dict[str, Any]] = {}
    async with server.session(token) as session:
        for question in QUESTIONS:
            found = body(await session.call_tool(
                "search_documents", {"query": question, "wind_farm_id": FARM, "limit": 5}
            ))  # fmt: skip
            for passage in found.get("passages", []):
                passages[passage["citation"]] = passage
        problems = []
        for citation, passage in passages.items():
            match = CITATION.match(citation)
            document = passage["document"]
            text = unwrap(wrapped(passage["text"]))
            if (
                match is None
                or match["document"] != document["document_id"]
                or int(match["revision"]) != document["revision"]
                or (document["document_id"], document["revision"]) not in known
                or hashlib.sha256(text.encode()).hexdigest() != passage["content_hash"]
                or not passage["current"]
            ):
                problems.append(citation)
                continue
            read = body(await session.call_tool(
                "read_passage", {"citation": citation, "wind_farm_id": FARM}
            ))["passage"]  # fmt: skip
            if (
                read["content_hash"] != passage["content_hash"]
                or unwrap(wrapped(read["text"])) != text
            ):
                problems.append(citation)
    documents = {p["document"]["document_id"] for p in passages.values()}
    check(
        len(passages) >= 10 and not problems,
        f"All {len(passages)} passages found for {len(QUESTIONS)} questions, from "
        f"{len(documents)} documents, are cited, in the manifest, current, and hold exactly their "
        "hashed text, read again by citation",
    )
    return sorted(passages)


async def check_research(server: Server, token: str) -> None:
    async with server.session(token) as session:
        found = body(await session.call_tool("search_web", {
            "topic": "severe_weather", "query": "Gale and storm warnings this week",
            "turbine_id": TURBINE, "max_results": 5,
        }))  # fmt: skip
        sources = found.get("sources", [])
        if found.get("state") == "unavailable":
            check(bool(found.get("data_gap")), f"Research is a recorded data gap: {found}")
            return
        complete = all(
            urlsplit(s["url"]).scheme == "https"
            and (urlsplit(s["url"]).hostname or "").removeprefix("www.") == s["domain"]
            and s["retrieved_at"]
            and set(s["flags"]) <= FLAGS
            and len(s["notes"]) == len(s["flags"])
            and wrapped(s["content"]).source == s["url"]
            for s in sources
        )
        check(
            bool(sources) and complete and found["outbound_query"] and found["provider"],
            f"A web search names what it sent and its {len(sources)} sources' addresses, "
            "domains, retrieval times and flags, each flag with its note",
        )
        again = body(await session.call_tool("get_research", {"research_id": found["research_id"]}))
        check(
            [s["source_id"] for s in again["sources"]] == [s["source_id"] for s in sources]
            and again["outbound_query"] == found["outbound_query"],
            "Read again, the search gives the same sources, as recorded",
        )


async def check_forecast(server: Server, token: str) -> None:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    async with server.session(token) as session:
        found = body(await session.call_tool("get_forecast", {
            "turbine_id": TURBINE, "from_time": start.isoformat(),
            "to_time": (start + timedelta(hours=6)).isoformat(),
        }))  # fmt: skip
    provenance = found.get("provenance") or {}
    check(
        found.get("state") in {"fresh", "stale"}
        and all(
            provenance.get(key)
            for key in (
                "provider",
                "licence",
                "retrieved_at",
                "valid_from",
                "valid_to",
                "content_hash",
            )
        )
        and all(point["valid_at"] for point in found.get("points", [])),
        f"A {found.get('state')} forecast names its provider, licence, retrieval time, valid span "
        "and hash",
    )


async def check_other_tenant(server: Server, token: str, citations: list[str]) -> None:
    theirs = {document for document, _ in manifest(DEMO)} - {d for d, _ in manifest(OTHER)}
    async with server.session(token) as session:
        seen: set[str] = set()
        for question in QUESTIONS:
            found = body(await session.call_tool("search_documents", {"query": question}))
            seen |= {p["document"]["document_id"] for p in found.get("passages", [])}
        check(
            bool(seen) and not seen & theirs,
            f"A user of {OTHER} finds {len(seen)} documents of their own, never one of {DEMO}'s",
        )
        own_only = [c for c in citations if CITATION.match(c)["document"] in theirs]  # type: ignore[index]
        answers = [
            body(await session.call_tool("read_passage", {"citation": citation}))
            for citation in own_only
        ]
        check(
            bool(own_only) and all(refused(answer) for answer in answers),
            f"Nor any of {len(own_only)} of {DEMO}'s own citations, which return nothing",
        )


async def check_scoped(servers: dict[str, Server], tokens: dict[str, str]) -> None:
    question = {"query": QUESTIONS[0]}
    async with servers["documentation"].session(tokens["documentation"]) as session:
        whole = body(await session.call_tool("search_documents", question))
        elsewhere = body(await session.call_tool(
            "search_documents", {**question, "wind_farm_id": OTHER_FARM}
        ))  # fmt: skip
        theirs = body(await session.call_tool(
            "search_documents", {**question, "wind_farm_id": FARM}
        ))  # fmt: skip
    check(
        refused(whole) and refused(elsewhere) and bool(theirs.get("passages")),
        f"Scoped to {FARM}, a user searches there, never the whole tenant or {OTHER_FARM}",
    )
    async with servers["tavily"].session(tokens["tavily"]) as session:
        research = body(await session.call_tool("search_web", {
            "topic": "severe_weather", "query": "Gale warnings", "turbine_id": OTHER_TURBINE,
        }))  # fmt: skip
    check(refused(research), f"Nor may they research a turbine of {OTHER_FARM}")


async def check_nobody(servers: dict[str, Server], tokens: dict[str, str]) -> None:
    start = datetime.now(UTC) + timedelta(hours=1)
    calls = (
        ("documentation", "search_documents", {"query": QUESTIONS[0], "wind_farm_id": FARM}),
        ("tavily", "search_web", {"topic": "forecast", "query": "Wind forecast"}),
        ("weather", "get_forecast", {"turbine_id": TURBINE, "from_time": start.isoformat(),
                                     "to_time": (start + timedelta(hours=2)).isoformat()}),
    )  # fmt: skip
    answers = []
    for server, tool, arguments in calls:
        async with servers[server].session(tokens[server]) as session:
            answers.append(body(await session.call_tool(tool, arguments)))
    check(
        all(refused(answer) for answer in answers),
        "A user with no role gets nothing from Documentation, Tavily or Weather",
    )


def sign_in(realm: Realm, email: str, password: str) -> tuple[str, str]:
    login = realm.login(email, password)
    if login is None:
        raise RuntimeError(f"{email} could not sign in")
    return str(jwt.decode(login.access, options={"verify_signature": False})["sub"]), login.access


def exchanged(caller: Caller, user: str, login: str) -> dict[str, str]:
    return {name: tokens_for(caller, user, login, name) for name in
            ("documentation", "tavily", "weather")}  # fmt: skip


async def prove(identity: Any, key: str, servers: dict[str, Server], realm: Realm,
                passwords: dict[str, str]) -> None:  # fmt: skip
    demo = read_demo_passwords()
    operator, operator_login = sign_in(realm, demo_email(DEMO), demo[DEMO])
    tokens = exchanged(Caller(identity, key, DEMO), operator, operator_login)
    citations = await check_citations(servers["documentation"], tokens["documentation"])
    await check_research(servers["tavily"], tokens["tavily"])
    await check_forecast(servers["weather"], tokens["weather"])

    stranger, stranger_login = sign_in(realm, demo_email(OTHER), demo[OTHER])
    other = exchanged(Caller(identity, key, OTHER), stranger, stranger_login)
    await check_other_tenant(servers["documentation"], other["documentation"], citations)

    scoped, scoped_login = sign_in(realm, SCOPED_EMAIL, passwords["scoped"])
    await check_scoped(servers, exchanged(Caller(identity, key, DEMO), scoped, scoped_login))
    nobody, nobody_login = sign_in(realm, NOBODY_EMAIL, passwords["nobody"])
    await check_nobody(servers, exchanged(Caller(identity, key, DEMO), nobody, nobody_login))


def user_id(api: httpx.Client, email: str) -> str:
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    return str(found.json()[0]["id"])


def main() -> int:
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    passwords = {"scoped": strong_password(), "nobody": strong_password()}
    grant, administrator = "", ""
    try:
        key, bundle = signing_key(), client_bundle()
        with (
            keycloak_admin() as base,
            port_forward("deployment/windops-authorization", SYSTEM, 50051) as authz_port,
            blocking_channel(authz_port, AUTHORIZATION_HOST, bundle) as authz_channel,
        ):
            authorization = SignedCaller(
                authorization_pb2_grpc.AuthorizationServiceStub(authz_channel), key
            )
            try:
                with admin_client(base, admin_password) as api:
                    organization = ensure_tenant(api, DEMO, TENANTS[DEMO])
                    made = {
                        name: ensure_user(api, email, passwords[name], new_password=True)
                        for name, email in (("scoped", SCOPED_EMAIL), ("nobody", NOBODY_EMAIL))
                    }
                    for member in made.values():
                        ensure_member(api, organization, member)
                    administrator = user_id(api, demo_admin_email(DEMO))
                    scoped_id = made["scoped"]
                grant = authorization.call("GrantRole", authz.GrantRoleRequest(
                    grantee_id=scoped_id, role="reliability-engineer",
                    scope=authz.Scope(wind_farm_id=FARM), idempotency_key=f"citations-{RUN}",
                ), administrator, DEMO).grant.grant_id  # fmt: skip
                with (
                    tls_files(bundle) as files,
                    keycloak_public(through_gateway=False) as public,
                    port_forward("deployment/windops-identity", SYSTEM, 50051) as port,
                    blocking_channel(port, HOST, bundle) as channel,
                    forwards(files) as servers,
                ):
                    identity = identity_pb2_grpc.IdentityServiceStub(channel)
                    asyncio.run(prove(identity, key, servers, Realm(public), passwords))
            finally:
                if grant:
                    authorization.call("RevokeRole", authz.RevokeRoleRequest(
                        grant_id=grant, idempotency_key=f"citations-revoke-{RUN}",
                    ), administrator, DEMO)  # fmt: skip
                with admin_client(base, admin_password) as api:
                    delete_user(api, SCOPED_EMAIL)
                    delete_user(api, NOBODY_EMAIL)
    except (
        RuntimeError, OSError, KeyError, IndexError, grpc.RpcError, httpx.HTTPError,
        jwt.PyJWTError, subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Citation check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
