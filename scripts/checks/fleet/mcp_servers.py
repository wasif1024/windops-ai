#!/usr/bin/env python3
"""Prove the first MCP servers on the real cluster (SRS 6.3, MCP-001, MCP-003, MCP-005).

Each server is called as the Agent Runtime will call it: over HTTPS with a certificate from the
cluster's CA, with a Keycloak token the Identity Service exchanged for that server and its tools,
through the official MCP client. The servers call the real services: Documentation searches the
demo tenant's documents, Tavily makes one real web search, and Weather reads a real forecast.

- no certificate, or no token, and nothing is answered;
- Documentation returns cited passages, wrapped as untrusted;
- Tavily returns what it sent, generic, with every web text wrapped, and advisory only;
- Weather returns a forecast with its provider, licence and age;
- Filesystem reads only its sandboxes, and flags the injected bulletin; PostgreSQL refuses an
  operator, and gives a Platform Administrator statistics only, never a query's text;
- a caller over a tool's rate is limited, counted in Redis;
- every call reaches the Audit Service, as the user who made it.

Run `make secrets`, `make infra`, `make services`, `make identity-seed` and `make knowledge-seed`
first. Needs local port 9443 free. It makes a temporary Platform Administrator, deleted after.
"""

import asyncio
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
import jwt
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from windops.identity.v1 import identity_pb2_grpc
from windops_identity.targets import MCP_TOOLS, mcp_audience, tool_scope
from windops_mcp import Untrusted, unwrap

from scripts.checks.consoles.platform_api import ADMIN_EMAIL, make_platform_administrator
from scripts.checks.consoles.platform_api import sign_in as sign_in_administrator
from scripts.checks.identity.identity import Realm, delete_user
from scripts.checks.identity.token_exchange import HOST, SYSTEM, Caller, check, signing_key
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import (
    ADMIN_REQUIRED_ACTIONS,
    ADMIN_SECRET_FILE,
    PLATFORM_TENANT,
    admin_client,
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

TENANT = "north-sea"
WIND_FARM, TURBINE = "dogger-east", "dogger-east-t01"
NAMESPACE = "windops-mcp"


def host(server: str) -> str:
    return f"{mcp_audience(server)}.{NAMESPACE}.svc"


class _Named(httpx.AsyncBaseTransport):
    """Checks the server's certificate against its own name, through a local port-forward."""

    def __init__(self, inner: httpx.AsyncBaseTransport, name: str) -> None:
        self._inner, self._name = inner, name

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.extensions["sni_hostname"] = self._name
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@contextmanager
def tls_files(bundle: ClientBundle) -> Iterator[tuple[str, str, str]]:
    """The check's certificate, key and CA as files, removed when done."""
    with tempfile.TemporaryDirectory() as folder:
        paths = []
        for name, data in (("tls.crt", bundle.certificate), ("tls.key", bundle.key),
                           ("ca.crt", bundle.authority)):  # fmt: skip
            path = os.path.join(folder, name)
            with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as file:
                file.write(data)
            paths.append(path)
        yield paths[0], paths[1], paths[2]


class Server:
    def __init__(self, name: str, port: int, files: tuple[str, str, str]) -> None:
        self.name, self.port, self.files = name, port, files
        self.url = f"https://127.0.0.1:{port}/mcp"

    def context(self, *, with_certificate: bool = True) -> ssl.SSLContext:
        certificate, key, authority = self.files
        made = ssl.create_default_context(cafile=authority)
        if with_certificate:
            made.load_cert_chain(certificate, key)
        return made

    def http(self, bearer: str | None, *, with_certificate: bool = True) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(verify=self.context(with_certificate=with_certificate))
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        return httpx.AsyncClient(
            transport=_Named(transport, host(self.name)), headers=headers, timeout=60
        )

    @asynccontextmanager
    async def session(self, bearer: str) -> AsyncIterator[ClientSession]:
        async with (
            self.http(bearer) as http,
            streamable_http_client(self.url, http_client=http, terminate_on_close=False) as (
                reader, writer, _,
            ),
            ClientSession(reader, writer) as session,
        ):  # fmt: skip
            await session.initialize()
            yield session


def body(result: CallToolResult) -> dict[str, Any]:
    if result.structuredContent is not None and not result.isError:
        return result.structuredContent
    content = result.content[0]
    if not isinstance(content, TextContent):
        raise RuntimeError("A tool answered with something other than text")
    found: dict[str, Any] = json.loads(content.text)
    return found


def wrapped(item: dict[str, Any]) -> Untrusted:
    return Untrusted.model_validate(item)


def tokens_for(caller: Caller, user: str, login: str, server: str) -> str:
    scopes = [tool_scope(server, tool) for tool in MCP_TOOLS[server]]
    answer = caller.exchange(user, login, mcp_audience(server), scopes)
    return str(answer.access_token)


async def check_token_needed(server: Server) -> None:
    async with server.http(None) as http:
        answer = await http.post(server.url, json={})
    check(answer.status_code == 401, "A client with a certificate but no token is refused")


async def check_certificate_needed(server: Server, token: str) -> None:
    """Last: kubectl's port-forward may stop once a handshake is refused."""
    refused = False
    try:
        async with server.http(token, with_certificate=False) as http:
            await http.post(server.url, json={})
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
        refused = True
    check(refused, "A client without the cluster's certificate cannot connect (MCP-001)")


async def check_documentation(server: Server, token: str) -> None:
    async with server.session(token) as session:
        listed = {tool.name for tool in (await session.list_tools()).tools}
        check(listed == {"search_documents", "read_passage"}, "Documentation lists its tools")
        found = body(await session.call_tool(
            "search_documents",
            {"query": "gearbox oil debris particle limits", "wind_farm_id": WIND_FARM},
        ))  # fmt: skip
        passages = found.get("passages", [])
        first = passages[0] if passages else {}
        check(
            bool(passages) and all(p["citation"].startswith("kn:") for p in passages)
            and wrapped(first["text"]).trust == "untrusted",
            f"Documentation finds {len(passages)} cited passages, each wrapped as untrusted",
        )  # fmt: skip
        read = body(await session.call_tool(
            "read_passage", {"citation": first["citation"], "wind_farm_id": WIND_FARM}
        ))  # fmt: skip
        check(
            read["passage"]["citation"] == first["citation"]
            and unwrap(wrapped(read["passage"]["text"])) == unwrap(wrapped(first["text"])),
            "Reading a passage by its citation gives the same text",
        )


async def check_tavily(server: Server, token: str) -> None:
    async with server.session(token) as session:
        found = body(await session.call_tool("search_web", {
            "topic": "severe_weather", "query": "Gale warnings for crews at Dogger East T01",
            "turbine_id": TURBINE, "max_results": 3,
        }))  # fmt: skip
        sent = found.get("outbound_query", "").lower()
        check(
            found.get("advisory_only") is True and "dogger" not in sent and "t01" not in sent
            and "turbine" in found.get("redactions", []),
            f"Tavily researched generic words only: {found.get('outbound_query', '')!r}",
        )  # fmt: skip
        sources = found.get("sources", [])
        check(
            found.get("state") == "unavailable"
            or (bool(sources) and all(wrapped(s["content"]).trust == "untrusted" for s in sources)),
            f"Its {len(sources)} sources each come back wrapped, with flags, or a data gap",
        )
        again = body(await session.call_tool("get_research", {"research_id": found["research_id"]}))
        check(again.get("outbound_query") == found.get("outbound_query"), "It reads as recorded")


async def check_weather(server: Server, token: str) -> None:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    async with server.session(token) as session:
        found = body(await session.call_tool("get_forecast", {
            "turbine_id": TURBINE, "from_time": start.isoformat(),
            "to_time": (start + timedelta(hours=6)).isoformat(),
        }))  # fmt: skip
        provenance = found.get("provenance") or {}
        check(
            found.get("state") in {"fresh", "stale"} and bool(found.get("points"))
            and provenance.get("provider") == "open-meteo" and bool(provenance.get("licence")),
            f"Weather gives a {found.get('state')} forecast from {provenance.get('provider')}, "
            f"{found.get('age_seconds')} seconds old, with its licence",
        )  # fmt: skip


async def check_filesystem(server: Server, token: str) -> None:
    async with server.session(token) as session:
        listed = body(await session.call_tool("list_files", {"root": "documents"}))
        names = {entry["name"] for entry in listed.get("entries", [])}
        check({"manifest.json", "oem", "evaluation"} <= names, "Filesystem lists the documents")
        read = body(await session.call_tool(
            "read_file", {"root": "documents", "path": "evaluation/adv-sb-inj-001-r1.md"}
        ))  # fmt: skip
        content = wrapped(read["content"])
        check(
            "instructions_to_ai" in content.flags and "SYSTEM OVERRIDE" in unwrap(content),
            "The injected bulletin comes back wrapped and flagged, its words only data",
        )
        escape = body(await session.call_tool(
            "read_file", {"root": "documents", "path": "../../../etc/passwd"}
        ))  # fmt: skip
        check(escape.get("error") == "refused", "A path out of the sandbox is refused")


async def check_postgres(server: Server, operator: str, administrator: str) -> None:
    async with server.session(operator) as session:
        refused = body(await session.call_tool("database_overview", {}))
        check(
            refused.get("error") == "refused" and "evaluation.manage" in refused.get("message", ""),
            "PostgreSQL diagnostics refuse an operator",
        )
    async with server.session(administrator) as session:
        overview = body(await session.call_tool("database_overview", {}))
        databases = {item["database"] for item in overview.get("databases", [])}
        check(
            {"windops_audit", "windops_knowledge"} <= databases,
            f"A Platform Administrator sees {len(databases)} service databases' statistics",
        )
        activity = body(await session.call_tool("connection_activity", {}))
        fields = {key for row in activity.get("connections", []) for key in row}
        check(
            bool(fields) and "query" not in fields,
            "Connection activity gives counts and ages, never a query's text",
        )


async def check_limits(server: Server, token: str) -> None:
    # A fixed window: start early in a minute, so the count is not cut by its end.
    while time.time() % 60 > 30:
        await asyncio.sleep(1)
    async with server.session(token) as session:
        for count in range(1, 70):
            answer = await session.call_tool("list_files", {"root": "evaluations"})
            if answer.isError:
                found = body(answer)
                check(
                    found.get("error") == "limited" and found.get("limit") == "per_minute",
                    f"Call {count} in a minute is limited, counted in Redis (MCP-003)",
                )
                return
    raise RuntimeError("Failed: no call was limited")


def audited(since: datetime) -> list[tuple[str, str, str]]:
    # The only value spliced in is this check's own clock reading.
    statement = (
        "SELECT set_config('windops.all_tenants', 'on', false); "  # noqa: S608
        "SELECT action || '|' || outcome || '|' || actor_id FROM audit_records "
        f"WHERE action LIKE 'mcp.%' AND recorded_at >= '{since.isoformat()}' "
        "ORDER BY recorded_at"
    )
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-data", "exec",
         "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
         "--no-align", "--username=windops_audit", "--dbname=windops_audit",
         "--set=ON_ERROR_STOP=1", f"--command={statement}"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    rows = [line.split("|") for line in result.stdout.splitlines() if line.count("|") == 2]
    return [(row[0], row[1], row[2]) for row in rows]


async def prove(caller: Caller, servers: dict[str, Server], realm: Realm, admin: str) -> None:
    started = datetime.now(UTC)
    login = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if login is None:
        raise RuntimeError("The demo operator could not sign in")
    user = str(jwt.decode(login.access, options={"verify_signature": False})["sub"])
    tokens = {name: tokens_for(caller, user, login.access, name) for name in MCP_TOOLS}
    await check_token_needed(servers["documentation"])
    await check_documentation(servers["documentation"], tokens["documentation"])
    await check_tavily(servers["tavily"], tokens["tavily"])
    await check_weather(servers["weather"], tokens["weather"])
    await check_filesystem(servers["filesystem"], tokens["filesystem"])
    admin_claims = jwt.decode(admin, options={"verify_signature": False})
    admin_caller = Caller(caller.stub, caller.key, tenant=PLATFORM_TENANT)
    admin_token = tokens_for(admin_caller, str(admin_claims["sub"]), admin, "postgres")
    await check_postgres(servers["postgres"], tokens["postgres"], admin_token)
    await check_limits(servers["filesystem"], tokens["filesystem"])
    deadline = time.monotonic() + 60
    expected = {
        ("mcp.documentation.search_documents", "succeeded"),
        ("mcp.tavily.search_web", "succeeded"),
        ("mcp.weather.get_forecast", "succeeded"),
        ("mcp.filesystem.read_file", "refused"),
        ("mcp.postgres.database_overview", "refused"),
        ("mcp.filesystem.list_files", "limited"),
    }
    while True:
        rows = audited(started)
        seen = {(action, outcome) for action, outcome, actor in rows if actor == user}
        if expected <= seen or time.monotonic() > deadline:
            break
        await asyncio.sleep(3)
    check(
        expected <= seen,
        f"All {len(rows)} calls reached the Audit Service, as the user who made each",
    )
    await check_certificate_needed(servers["weather"], tokens["weather"])


@contextmanager
def forwards(files: tuple[str, str, str]) -> Iterator[dict[str, Server]]:
    with ExitStack() as stack:
        servers = {}
        for name in MCP_TOOLS:
            port = stack.enter_context(
                port_forward(f"deployment/{mcp_audience(name)}", NAMESPACE, 8443)
            )
            servers[name] = Server(name, port, files)
        yield servers


def main() -> int:
    admin_password = read_passwords(ADMIN_SECRET_FILE)["password"]
    password = strong_password()
    try:
        with keycloak_admin() as base:
            try:
                with admin_client(base, admin_password) as api:
                    admin_id = ensure_user(
                        api, ADMIN_EMAIL, password, new_password=True,
                        required_actions=ADMIN_REQUIRED_ACTIONS,
                    )  # fmt: skip
                    ensure_member(
                        api, ensure_tenant(api, PLATFORM_TENANT, "WindOps Platform"), admin_id
                    )
                make_platform_administrator(admin_id)
                administrator, _ = sign_in_administrator(password)
                bundle, key = client_bundle(), signing_key()
                with (
                    tls_files(bundle) as files,
                    keycloak_public() as public,
                    port_forward("deployment/windops-identity", SYSTEM, 50051) as port,
                    blocking_channel(port, HOST, bundle) as channel,
                    forwards(files) as servers,
                ):
                    caller = Caller(identity_pb2_grpc.IdentityServiceStub(channel), key)
                    asyncio.run(prove(caller, servers, Realm(public), administrator))
            finally:
                with admin_client(base, admin_password) as api:
                    delete_user(api, ADMIN_EMAIL)
    except (
        RuntimeError, OSError, KeyError, grpc.RpcError, httpx.HTTPError, jwt.PyJWTError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("MCP servers check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
