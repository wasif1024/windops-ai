#!/usr/bin/env python3
"""Prove WindOps MCP and the agents' allowlists on the cluster (M8.6).

A real user signs in, and the Investigation Service's certificate starts a delegation for them.
The Agent Runtime's certificate then mints each agent's tokens, which the real WindOps MCP server
answers from the real services. The check proves:

- each agent gets tokens for the tools on its allowlist, and none for any other (MCP-002), so text
  an agent reads can never widen it (MCP-007);
- no agent that reads external content may hold a write tool (MCP-006);
- WindOps MCP answers each agent from the services, in the user's tenant only: turbines,
  telemetry, alarms, cases and evidence, a deterministic impact estimate, and a policy check;
- a token for one tool calls no other, and another tenant's turbine is not found;
- every call is audited as the agent, on the user's behalf (SEC-010).

Run `make secrets`, `make infra`, `make migrate`, `make services` and `make identity-seed` first.
The check opens a case of its own on the demo turbine, and cancels it at the end. Needs local port
9443 free.
"""

import asyncio
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any

import grpc
import httpx
import jwt
from windops.identity.v1 import identity_pb2 as pb
from windops.investigation.v1 import investigation_pb2, investigation_pb2_grpc
from windops_identity.targets import AGENT_TOOLS, EXTERNAL_TOOLS, WRITE_TOOLS

from scripts.checks.agents.runtime import investigation_bundle
from scripts.checks.fleet.asset import Caller, bootstrap
from scripts.checks.fleet.mcp_servers import Server, body, tls_files
from scripts.checks.identity.agent_identity import (
    as_service,
    as_user,
    audited,
    bundle_of,
    code_of,
    identity_as,
)
from scripts.checks.identity.identity import Realm
from scripts.checks.identity.token_exchange import TENANT, check, signing_key
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import demo_email, keycloak_public, read_demo_passwords

WINDOPS = "windops-mcp-windops"
INVESTIGATION_HOST = "windops-investigation.windops-application.svc.cluster.local"
ENGINEER = "agent-tools-check-engineer"
TURBINE, OTHER_TURBINE = "dogger-east-t01", "atlas-ridge-t01"
CASE = "case_" + "j" * 20
RUN = str(int(time.time()))


def scope(tool: str) -> str:
    return f"mcp.windops.{tool}"


async def call(server: Server, token: str, tool: str, arguments: dict[str, Any]) -> Any:
    async with server.session(token) as session:
        return await session.call_tool(tool, arguments)


def answer(server: Server, token: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(call(server, token, tool, arguments))
    if result.isError:
        raise RuntimeError(f"{tool} failed: {body(result)}")
    return body(result)


def refused(server: Server, token: str, tool: str, arguments: dict[str, Any]) -> bool:
    try:
        result = asyncio.run(call(server, token, tool, arguments))
    except Exception:  # Any refusal: the server or the MCP client may raise.
        return True
    return bool(result.isError)


class Cases:
    """Opens the check's own case as the demo operator, and cancels it afterwards."""

    def __init__(self, key: str) -> None:
        self._key, self._stack = key, ExitStack()
        port = self._stack.enter_context(
            port_forward("deployment/windops-investigation", "windops-application", 50051)
        )
        channel = self._stack.enter_context(
            blocking_channel(port, INVESTIGATION_HOST, client_bundle())
        )
        self._api = Caller(investigation_pb2_grpc.InvestigationServiceStub(channel), key)
        self.opened: list[str] = []

    def open(self, user: str) -> str:
        request = investigation_pb2.CreateCaseRequest(
            turbine_id=TURBINE, title=f"Agent tools check {RUN}", severity="medium",
            idempotency_key=f"tools-check-{RUN}",
        )  # fmt: skip
        case_id = str(self._api.call("CreateCase", request, user, TENANT).case.case_id)
        self.opened.append(case_id)
        return case_id

    def close(self) -> None:
        try:
            if self.opened:
                bootstrap(TENANT, ENGINEER, "reliability-engineer")
            for case_id in self.opened:
                self._api.call(
                    "CancelCase",
                    investigation_pb2.CancelCaseRequest(case_id=case_id, reason="Check over"),
                    ENGINEER, TENANT,
                )  # fmt: skip
        finally:
            self._stack.close()


def wait_for_evidence(server: Server, token: str, case_id: str) -> dict[str, Any]:
    """The evidence builder freezes a new case's snapshot within a minute or so."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        case: dict[str, Any] = answer(server, token, "get_case", {"case_id": case_id})["case"]
        if case["evidence_snapshot_id"]:
            return case
        time.sleep(5)
    raise RuntimeError(f"Case {case_id}'s evidence was never frozen")


def prove(realm: Realm, key: str, server: Server, cases: "Cases") -> None:
    started = datetime.now(UTC)
    check(
        not any(t & EXTERNAL_TOOLS and t & WRITE_TOOLS for t in AGENT_TOOLS.values()),
        "No agent that reads external content holds a write tool (MCP-006)",
    )
    login = realm.login(demo_email(TENANT), read_demo_passwords()[TENANT])
    if login is None:
        raise RuntimeError("The demo operator could not sign in")
    user = str(jwt.decode(login.access, options={"verify_signature": False})["sub"])
    with identity_as(investigation_bundle()) as identity:
        delegation = identity.StartDelegation(
            pb.StartDelegationRequest(case_id=CASE, subject_token=login.access,
                                      idempotency_key=f"tools-check-{RUN}"),
            **as_user(user, key),
        ).delegation.delegation_id  # fmt: skip

    runtime = bundle_of("windops-agent-runtime-tls", "windops-application")
    with identity_as(runtime) as identity:

        def mint(agent: str, audience: str, scopes: list[str]) -> Any:
            return identity.IssueAgentToken(
                pb.IssueAgentTokenRequest(delegation_id=delegation, agent=agent,
                                          audience=audience, scopes=scopes),
                **as_service("service:agent-runtime", key),
            )  # fmt: skip

        allowed, denied = 0, 0
        for agent, tools in AGENT_TOOLS.items():
            for tool in sorted(tools):
                server_name = tool.split(".")[1]
                mint(agent, f"windops-mcp-{server_name}", [tool])
                allowed += 1
            for tool in sorted(
                t
                for t in (
                    scope(n) for n in ("get_turbine", "get_case", "estimate_impact", "check_policy")
                )
                if t not in tools
            ):
                if (
                    code_of(lambda a=agent, t=tool: mint(a, WINDOPS, [t]))
                    != grpc.StatusCode.PERMISSION_DENIED
                ):
                    raise RuntimeError(f"{agent} got {tool}, which is not on its allowlist")
                denied += 1
            if (
                "mcp.tavily.search_web" not in tools
                and code_of(
                    lambda a=agent: mint(a, "windops-mcp-tavily", ["mcp.tavily.search_web"])
                )
                != grpc.StatusCode.PERMISSION_DENIED
            ):
                raise RuntimeError(f"{agent} got web search, which is not on its allowlist")
        check(
            True,
            f"Each agent gets every tool on its allowlist ({allowed} in all), and is refused "
            f"{denied} WindOps tools and web search off it (MCP-002, MCP-007)",
        )

        def token(agent: str, *tools: str) -> str:
            return str(mint(agent, WINDOPS, [scope(t) for t in tools]).access_token)

        scada = token("scada-analyst", "get_turbine", "get_latest_telemetry", "list_alarms")
        turbine = answer(server, scada, "get_turbine", {"turbine_id": TURBINE})
        telemetry = answer(server, scada, "get_latest_telemetry", {"turbine_id": TURBINE})
        alarms = answer(server, scada, "list_alarms", {"turbine_id": TURBINE, "limit": 5})
        check(
            turbine["model"] and telemetry["points"] and "alarms" in alarms,
            f"The SCADA Analyst reads {TURBINE}: {turbine['model']}, "
            f"{len(telemetry['points'])} metrics, {len(alarms['alarms'])} recent alarms",
        )
        check(
            refused(server, scada, "get_turbine", {"turbine_id": OTHER_TURBINE}),
            "Another tenant's turbine is not found: the tenant comes from the token",
        )
        check(
            refused(server, scada, "get_case", {"case_id": CASE}),
            "A token for some tools calls no other",
        )
        supervisor = token("supervisor", "list_cases", "get_case", "get_evidence", "check_policy")
        case_id = cases.open(user)
        cases_seen = answer(server, supervisor, "list_cases", {"turbine_id": TURBINE})["cases"]
        case = wait_for_evidence(server, supervisor, case_id)
        evidence = answer(server, supervisor, "get_evidence",
                          {"snapshot_id": case["evidence_snapshot_id"], "limit": 5})  # fmt: skip
        check(
            evidence["case_id"] == case["case_id"] and evidence["items"],
            f"The Supervisor reads {len(cases_seen)} cases and case {case['case_id']}'s frozen "
            f"evidence: {evidence['item_count']} items, verified {evidence['verified']}",
        )
        impact_token = token("production-impact", "estimate_impact")
        impact = answer(server, impact_token, "estimate_impact",
                        {"case_id": case["case_id"], "action": "inspection",
                         "downtime_hours": 6})  # fmt: skip
        again = answer(server, impact_token, "estimate_impact",
                       {"case_id": case["case_id"], "action": "inspection",
                        "downtime_hours": 6})  # fmt: skip
        check(
            impact["figures"] and again["result_id"] == impact["result_id"],
            f"The Production Impact agent gets the service's own estimate, {impact['result_id']}, "
            "with ranges, and a retry returns the same result",
        )
        policy = answer(server, supervisor, "check_policy", {
            "case_id": case["case_id"], "action": "inspection",
            "summary": "Inspect the gearbox oil and filter", "requires_climb": True,
            "impact_result_id": impact["result_id"],
        })  # fmt: skip
        check(
            policy["outcome"] in ("pass", "escalate", "block") and policy["rules"],
            f"The Supervisor's policy check is the Policy Service's: {policy['outcome']}, by "
            f"{len(policy['rules'])} rules",
        )

    wanted = {
        ("mcp.windops.get_turbine", "agent", "agent:scada-analyst", user),
        ("mcp.windops.get_evidence", "agent", "agent:supervisor", user),
        ("mcp.windops.estimate_impact", "agent", "agent:production-impact", user),
        ("mcp.windops.check_policy", "agent", "agent:supervisor", user),
    }
    deadline, rows = time.monotonic() + 60, []
    while time.monotonic() < deadline:
        rows = audited(started)
        if wanted <= set(rows):
            break
        time.sleep(3)
    check(wanted <= set(rows), "Every call was audited as the agent, on the user's behalf")


def main() -> int:
    try:
        bundle, key = client_bundle(), signing_key()
        with ExitStack() as stack:
            files = stack.enter_context(tls_files(bundle))
            public = stack.enter_context(keycloak_public())
            port = stack.enter_context(port_forward(f"deployment/{WINDOPS}", "windops-mcp", 8443))
            cases = Cases(key)
            stack.callback(cases.close)
            prove(Realm(public), key, Server("windops", port, files), cases)
    except (
        RuntimeError, OSError, KeyError, grpc.RpcError, httpx.HTTPError, jwt.PyJWTError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Agent tools check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
