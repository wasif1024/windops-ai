#!/usr/bin/env python3
"""Prove agent identities on the cluster: delegations, agent tokens, and what agents may do (M8.5).

A real user signs in at Keycloak. Each call is made with the certificate of the service that makes
it on the platform, read from the cluster: the Investigation Service's to start a delegation, the
Agent Runtime's to mint its agents' tokens. Tokens are then used at the real Documentation MCP
server. The check proves:

- a user delegates with their own current token, and an agent gets a 120-second token for one MCP
  server and named tools, naming the user as its subject and the agent in "act" (SEC-008);
- the tenant's fault-investigator is delegated to only by the Investigation Service, as itself;
- the Documentation MCP server accepts both, and audits each call as the agent, on the
  principal's behalf (SEC-010);
- whatever the user may do, their agent may only read and draft, and fault-investigator likewise
  (SEC-009), as the real Authorization Service decides;
- no other service may start a delegation or mint its tokens, a token is refused at any other
  server, another tenant cannot use the delegation, and an ended one mints nothing more;
- only the Identity Service's pod mounts the key agent tokens are signed with.

Run `make secrets`, `make migrate`, `make services`, `make identity-seed` and `make knowledge-seed`
first. Needs local port 9443 free.
"""

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import Any

import grpc
import httpx
import jwt
from windops.authorization.v1 import authorization_pb2, authorization_pb2_grpc
from windops.identity.v1 import identity_pb2 as pb
from windops.identity.v1 import identity_pb2_grpc
from windops_auth import VerifiedIdentity, sign_identity

from scripts.checks.agents.runtime import investigation_bundle
from scripts.checks.fleet.mcp_servers import Server, body, tls_files
from scripts.checks.identity.identity import Realm
from scripts.checks.identity.token_exchange import HOST, SYSTEM, TENANT, check, signing_key
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import demo_email, keycloak_public, read_demo_passwords

OTHER = "atlas-wind"
AUTHORIZATION_HOST = "windops-authorization.windops-system.svc.cluster.local"
DOCUMENTATION, WEATHER = "windops-mcp-documentation", "windops-mcp-weather"
SEARCH = "mcp.documentation.search_documents"
CASE = "case_" + "i" * 20
RUN = str(int(time.time()))
KEY_SECRET = "windops-agent-token-key"  # noqa: S105  # A Secret's name, not a key.


def bundle_of(secret: str, namespace: str) -> ClientBundle:
    import base64

    data = json.loads(
        subprocess.run(
            ["kubectl", "--context", KUBE_CONTEXT, f"--namespace={namespace}", "get", "secret",
             secret, "--output=json"],
            capture_output=True, text=True, check=True,
        ).stdout
    )["data"]  # fmt: skip
    return ClientBundle(
        base64.b64decode(data["tls.crt"]),
        base64.b64decode(data["tls.key"]),
        base64.b64decode(data["ca.crt"]),
    )


def as_user(user: str, key: str, tenant: str = TENANT, actor: str | None = None) -> Any:
    identity = VerifiedIdentity(subject=user, tenant_id=tenant, user_id=user, actor=actor)
    return {"metadata": (("x-windops-identity", sign_identity(identity, key, ttl_seconds=60)),),
            "timeout": 15}  # fmt: skip


def as_service(subject: str, key: str, tenant: str = TENANT, actor: str | None = None) -> Any:
    identity = VerifiedIdentity(subject=subject, tenant_id=tenant, actor=actor)
    return {"metadata": (("x-windops-identity", sign_identity(identity, key, ttl_seconds=60)),),
            "timeout": 15}  # fmt: skip


def code_of(call: Any) -> grpc.StatusCode:
    try:
        call()
    except grpc.RpcError as error:
        status: grpc.StatusCode = error.code()
        return status
    return grpc.StatusCode.OK


def audited(since: datetime) -> list[tuple[str, str, str, str]]:
    # The only value spliced in is this check's own clock reading.
    statement = (
        "SELECT set_config('windops.all_tenants', 'on', false); "  # noqa: S608
        "SELECT action || '|' || actor_type || '|' || actor_id || '|' || on_behalf_of "
        f"FROM audit_records WHERE action LIKE 'mcp.%' AND recorded_at >= '{since.isoformat()}'"
    )
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "--namespace=windops-data", "exec",
         "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
         "--no-align", "--username=windops_audit", "--dbname=windops_audit",
         "--set=ON_ERROR_STOP=1", f"--command={statement}"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    rows = [line.split("|") for line in result.stdout.splitlines() if line.count("|") == 3]
    return [(r[0], r[1], r[2], r[3]) for r in rows]


@contextmanager
def identity_as(bundle: ClientBundle) -> Iterator[Any]:
    with (
        port_forward("deployment/windops-identity", SYSTEM, 50051) as port,
        blocking_channel(port, HOST, bundle) as channel,
    ):
        yield identity_pb2_grpc.IdentityServiceStub(channel)


def check_key_mount() -> None:
    deployments = json.loads(
        subprocess.run(
            ["kubectl", "--context", KUBE_CONTEXT, "get", "deployments", "--all-namespaces",
             "--output=json"], capture_output=True, text=True, check=True,
        ).stdout
    )["items"]  # fmt: skip
    mounting = sorted(
        d["metadata"]["name"] for d in deployments
        if any((v.get("secret") or {}).get("secretName") == KEY_SECRET
               for v in d["spec"]["template"]["spec"].get("volumes", []))
    )  # fmt: skip
    check(mounting == ["windops-identity"], "Only the Identity Service's pod mounts the agent key")


async def search(server: Server, token: str) -> int:
    async with server.session(token) as session:
        found = body(await session.call_tool(
            "search_documents",
            {"query": "gearbox oil debris particle limits", "wind_farm_id": "dogger-east"},
        ))  # fmt: skip
    return len(found.get("passages", []))


async def refused_at(server: Server, token: str) -> int:
    async with server.http(token) as http:
        answer = await http.post(
            server.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
    return answer.status_code


def check_authorization(user: str, key: str, bundle: ClientBundle) -> None:
    with (
        port_forward("deployment/windops-authorization", SYSTEM, 50051) as port,
        blocking_channel(port, AUTHORIZATION_HOST, bundle) as channel,
    ):
        stub = authorization_pb2_grpc.AuthorizationServiceStub(channel)

        def allowed(permission: str, options: dict[str, Any]) -> bool:
            request = authorization_pb2.CheckRequest(permission=permission)
            return bool(stub.Check(request, **options).allowed)

        person = as_user(user, key)
        agent = as_user(user, key, actor="agent:scada-analyst")
        check(
            allowed("alarm.acknowledge", person) and not allowed("alarm.acknowledge", agent)
            and allowed("alarm.read", agent),
            "The operator may acknowledge an alarm; their agent may only read it (SEC-009)",
        )  # fmt: skip
        investigator = as_service("service:fault-investigator", key, actor="agent:supervisor")
        check(
            allowed("telemetry.read", investigator) and allowed("maintenance.draft", investigator)
            and not allowed("maintenance.submit", investigator)
            and not allowed("maintenance.approve", investigator),
            "fault-investigator reads evidence and drafts, and never submits or approves",
        )  # fmt: skip


def prove(realm: Realm, key: str, server: Server, weather: Server) -> None:
    started = datetime.now(UTC)
    login = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if login is None:
        raise RuntimeError("The demo operator could not sign in")
    user = str(jwt.decode(login.access, options={"verify_signature": False})["sub"])
    check_key_mount()
    investigation = investigation_bundle()
    runtime = bundle_of("windops-agent-runtime-tls", "windops-application")
    stranger = client_bundle()

    with identity_as(investigation) as identity:
        mine = identity.StartDelegation(
            pb.StartDelegationRequest(case_id=CASE, subject_token=login.access,
                                      idempotency_key=f"agent-check-{RUN}-user"),
            **as_user(user, key),
        ).delegation  # fmt: skip
        faults = identity.StartDelegation(
            pb.StartDelegationRequest(case_id=CASE, idempotency_key=f"agent-check-{RUN}-fault"),
            **as_service("service:investigation", key),
        ).delegation  # fmt: skip
        check(
            mine.principal == f"user:{user}" and faults.principal == "service:fault-investigator"
            and mine.expires_at.seconds - mine.created_at.seconds == 3600,
            "A user delegates with their own token, and the Investigation Service, as itself, "
            "for the tenant's fault-investigator: each for an hour",
        )  # fmt: skip
        no_token = code_of(lambda: identity.StartDelegation(
            pb.StartDelegationRequest(case_id=CASE, idempotency_key=f"agent-check-{RUN}-x"),
            **as_user(user, key),
        ))  # fmt: skip
        check(
            no_token == grpc.StatusCode.PERMISSION_DENIED,
            "A user cannot delegate as the fault-investigator",
        )
        minted_here = code_of(lambda: identity.IssueAgentToken(
            pb.IssueAgentTokenRequest(delegation_id=mine.delegation_id, agent="supervisor",
                                      audience=DOCUMENTATION, scopes=[SEARCH]),
            **as_service("service:investigation", key),
        ))  # fmt: skip

    with identity_as(runtime) as identity:

        def mint(delegation: str, agent: str, tenant: str = TENANT) -> Any:
            return identity.IssueAgentToken(
                pb.IssueAgentTokenRequest(delegation_id=delegation, agent=agent,
                                          audience=DOCUMENTATION, scopes=[SEARCH]),
                **as_service("service:agent-runtime", key, tenant),
            )  # fmt: skip

        for_user = mint(mine.delegation_id, "reliability-engineer")
        for_faults = mint(faults.delegation_id, "supervisor")
        claims = jwt.decode(for_user.access_token, options={"verify_signature": False})
        check(
            claims["sub"] == user and claims["act"] == {"sub": "agent:reliability-engineer"}
            and claims["aud"] == DOCUMENTATION and claims["scope"] == SEARCH
            and claims["exp"] - claims["iat"] == 120 and claims["tenant_id"] == TENANT,
            "The Agent Runtime gets a 120-second token for one agent, one server and one tool, "
            "naming the user and the agent (SEC-008)",
        )  # fmt: skip
        found = asyncio.run(search(server, for_user.access_token))
        found_too = asyncio.run(search(server, for_faults.access_token))
        check(
            found > 0 and found_too > 0,
            f"Documentation answers both agents: {found} passages for the user's, {found_too} "
            "for fault-investigator's",
        )
        elsewhere = asyncio.run(refused_at(weather, for_user.access_token))
        check(elsewhere == 401, "The same token is refused at any other MCP server")
        other_tenant = code_of(lambda: mint(mine.delegation_id, "supervisor", OTHER))
        check(other_tenant == grpc.StatusCode.NOT_FOUND, "Another tenant cannot use the delegation")
        started_here = code_of(lambda: identity.StartDelegation(
            pb.StartDelegationRequest(case_id=CASE, idempotency_key=f"agent-check-{RUN}-y"),
            **as_service("service:investigation", key),
        ))  # fmt: skip
        identity.EndDelegation(
            pb.EndDelegationRequest(delegation_id=mine.delegation_id, reason="Run completed",
                                    idempotency_key=f"agent-check-{RUN}-end"),
            **as_service("service:agent-runtime", key),
        )  # fmt: skip
        after_end = code_of(lambda: mint(mine.delegation_id, "reliability-engineer"))
        check(
            after_end == grpc.StatusCode.PERMISSION_DENIED,
            "An ended delegation mints nothing more",
        )

    with identity_as(stranger) as identity:
        stranger_mint = code_of(lambda: identity.IssueAgentToken(
            pb.IssueAgentTokenRequest(delegation_id=faults.delegation_id, agent="supervisor",
                                      audience=DOCUMENTATION, scopes=[SEARCH]),
            **as_service("service:agent-runtime", key),
        ))  # fmt: skip
    check(
        {minted_here, stranger_mint, started_here} == {grpc.StatusCode.PERMISSION_DENIED},
        "Only the Agent Runtime mints, and only Web BFF and Investigation delegate, by certificate",
    )

    deadline, wanted = time.monotonic() + 60, {
        ("mcp.documentation.search_documents", "agent", "agent:reliability-engineer", user),
        ("mcp.documentation.search_documents", "agent", "agent:supervisor",
         "service:fault-investigator"),
    }  # fmt: skip
    rows: list[tuple[str, str, str, str]] = []
    while time.monotonic() < deadline:
        rows = audited(started)
        if wanted <= set(rows):
            break
        time.sleep(3)
    check(
        wanted <= set(rows),
        "Each call reached the Audit Service as the agent, on the principal's behalf (SEC-010)",
    )
    check_authorization(user, key, stranger)


def main() -> int:
    try:
        bundle, key = client_bundle(), signing_key()
        with ExitStack() as stack:
            files = stack.enter_context(tls_files(bundle))
            public = stack.enter_context(keycloak_public())
            servers = {
                name: Server(
                    name.removeprefix("windops-mcp-"),
                    stack.enter_context(port_forward(f"deployment/{name}", "windops-mcp", 8443)),
                    files,
                )
                for name in (DOCUMENTATION, WEATHER)
            }
            prove(Realm(public), key, servers[DOCUMENTATION], servers[WEATHER])
    except (
        RuntimeError, OSError, KeyError, grpc.RpcError, httpx.HTTPError, jwt.PyJWTError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Agent identity check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
