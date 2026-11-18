#!/usr/bin/env python3
"""Prove the External Intelligence Service on the cluster, with real Tavily searches (M7.4).

Run `make secrets` with TAVILY_API_KEY in .env, then `make migrate` and the service. Calls are
signed as Web BFF signs them, and every decision comes from the real Authorization Service. The
check proves:

- a search about a demo turbine goes out generic: none of the tenant's, site's or turbine's names,
  the site as its region and the turbine as its class (EXT-006); it comes back recorded, with each
  source's URL, times and flags (EXT-003, EXT-004), and its full content can be extracted;
- another tenant cannot find it;
- in a tenant of its own, `knowledge-check`: research turned off, or over its daily limit, is a
  recorded data gap, and the provider is not asked (EGR-002, EXT-005); a password alone cannot
  change the policy;
- PostgreSQL refuses to change kept research, and hides it from another tenant;
- only this service's pod mounts the provider's key, and its logs never hold a query.

Its research and policy are removed at the end.
"""

import json
import secrets
import subprocess
import sys
import time
from typing import Any

import grpc
from windops.external_intelligence.v1 import external_intelligence_pb2 as pb
from windops.external_intelligence.v1 import external_intelligence_pb2_grpc
from windops_auth import LEVEL_PASSWORD

from scripts.checks.fleet.asset import Caller, bootstrap, check, kubectl, signing_key
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-external-intelligence.windops-application.svc.cluster.local"
DATABASE = "windops_external_intelligence"
DEMO, OTHER, TENANT = "north-sea", "atlas-wind", "knowledge-check"
ENGINEER, ADMIN, STRANGER = (
    "research-check-engineer",
    "research-check-admin",
    "research-check-stranger",
)
RUN = secrets.token_hex(3)
FLAGS = {"stale", "undated", "low_confidence", "conflicting", "instructions_to_ai"}
QUESTION = "Storm and gale warnings affecting crews at Dogger East T01 this week"


def sql(statement: str, tenant: str = "") -> subprocess.CompletedProcess[str]:
    setting = (
        f"SELECT set_config('windops.tenant_id', '{tenant}', false); "
        if tenant
        else "SELECT set_config('windops.all_tenants', 'on', false); "
    )
    return kubectl(
        "exec",
        "windops-postgres-0",
        "--",
        "psql",
        "--no-psqlrc",
        "--quiet",
        "--tuples-only",
        "--no-align",
        f"--username={DATABASE}",
        f"--dbname={DATABASE}",
        "--set=ON_ERROR_STOP=1",
        f"--command={setting}{statement}",
        namespace=DATA,
    )


def value(statement: str, tenant: str = "") -> str:
    result = sql(statement, tenant)
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def research(caller: Caller, user: str, tenant: str, **fields: Any) -> Any:
    request = pb.ResearchRequest(**fields)
    return caller.call("Research", request, user, tenant).research


def check_demo(caller: Caller) -> Any:
    found = research(
        caller,
        ENGINEER,
        DEMO,
        topic="severe_weather",
        query=QUESTION,
        turbine_id="dogger-east-t01",
        max_results=5,
    )
    sent = found.outbound_query.lower()
    check(
        not any(name in sent for name in ("dogger", "north sea", "north-sea", "t01", "ws-8"))
        and "8 mw offshore wind turbine" in sent
        and "united kingdom" in sent
        and "turbine" in found.redactions,
        f"The query goes out generic: {found.outbound_query!r}",
    )
    check(
        found.state == "complete"
        and found.provider == "tavily"
        and bool(found.provider_request_id)
        and len(found.sources) > 0,
        f"Tavily answers with {len(found.sources)} sources in {found.response_seconds:.2f} s",
    )
    for source in found.sources:
        check(
            source.url.startswith("https://")
            and source.retrieved_at.seconds > 0
            and set(source.flags) <= FLAGS
            and len(source.notes) == len(source.flags)
            and (source.HasField("published_at") or "undated" in source.flags),
            f"{source.source_id}: {source.domain}, flags {list(source.flags) or 'none'}"
            f"{', trusted' if source.trusted else ''}",
        )
    bulletin = research(
        caller,
        ENGINEER,
        DEMO,
        topic="oem_bulletin",
        max_results=3,
        query="Gearbox oil cooler fan failures in wind turbines",
    )
    check(
        bulletin.state == "complete" and bulletin.redactions == [],
        f"An OEM bulletin search finds {len(bulletin.sources)} sources",
    )
    extracted = caller.call(
        "Extract",
        pb.ExtractRequest(research_id=found.research_id, source_ids=[found.sources[0].source_id]),
        ENGINEER,
        DEMO,
    )
    page = extracted.extractions[0]
    check(
        extracted.state == "complete"
        and page.state in ("extracted", "failed")
        and (page.state == "failed" or len(page.content) > 0),
        f"Its first source is extracted: {page.state}, {len(page.content)} characters",
    )
    kept = caller.call(
        "GetResearch", pb.GetResearchRequest(research_id=found.research_id), ENGINEER, DEMO
    ).research
    check(
        kept.research_id == found.research_id and len(kept.extractions) == 1,
        "The search and its extraction read back as recorded",
    )
    check(
        caller.code(
            "GetResearch", pb.GetResearchRequest(research_id=found.research_id), STRANGER, OTHER
        )
        == grpc.StatusCode.NOT_FOUND,
        "Another tenant cannot find it",
    )
    return found


def check_policy(caller: Caller) -> None:
    def policy(user: str = ADMIN, **fields: Any) -> Any:
        request = pb.SetResearchPolicyRequest(
            **{"enabled": True, "daily_limit": 200, "reason": "Research check", **fields}
        )
        return request, user

    request, _ = policy(enabled=False)
    check(
        caller.code("SetResearchPolicy", request, ADMIN, TENANT, level=LEVEL_PASSWORD)
        == grpc.StatusCode.PERMISSION_DENIED,
        "A password alone cannot change the policy",
    )
    caller.call("SetResearchPolicy", request, ADMIN, TENANT)
    off = research(caller, ENGINEER, TENANT, topic="industry", query="offshore wind news")
    check(
        off.state == "unavailable" and "turned off" in off.data_gap,
        f"Turned off, research is a data gap: {off.data_gap}",
    )
    request, _ = policy(daily_limit=1)
    caller.call("SetResearchPolicy", request, ADMIN, TENANT)
    first = research(
        caller, ENGINEER, TENANT, topic="industry", query="offshore wind news", max_results=1
    )
    over = research(caller, ENGINEER, TENANT, topic="industry", query="offshore wind news")
    check(
        first.state == "complete" and over.state == "unavailable" and "quota" in over.data_gap,
        f"Over its daily limit, too: {over.data_gap}",
    )
    asked = value(
        "SELECT string_agg(provider_called::text, ',' ORDER BY requested_at) FROM research", TENANT
    )
    check(asked == "false,true,false", "The provider was asked only within the policy")


def check_database(found: Any) -> None:
    if not found.research_id.startswith("res_") or not found.research_id[4:].isalnum():
        raise RuntimeError(f"Unexpected research ID: {found.research_id!r}")
    for statement in (
        f"UPDATE research SET query = 'changed' WHERE research_id = '{found.research_id}'",  # noqa: S608  # The ID is checked above.
        f"DELETE FROM research WHERE research_id = '{found.research_id}'",  # noqa: S608
        f"UPDATE extractions SET content = '' WHERE research_id = '{found.research_id}'",  # noqa: S608
    ):
        refused = sql(statement, DEMO)
        check(
            refused.returncode != 0 and "never changes" in refused.stderr,
            f"PostgreSQL refuses: {statement.split(' WHERE')[0]}",
        )
    hidden = value(
        f"SELECT count(*) FROM research WHERE research_id = '{found.research_id}'",  # noqa: S608
        OTHER,
    )
    check(hidden == "0", "Row-level security hides it from another tenant")


def check_isolation(started: float) -> None:
    deployments = json.loads(
        kubectl(
            "get", "deployments", "--all-namespaces", "--output=json", namespace=APPLICATION
        ).stdout
    )
    mounting = sorted(
        item["metadata"]["name"]
        for item in deployments["items"]
        if "windops-tavily-api-key" in json.dumps(item["spec"]["template"]["spec"])
    )
    check(
        mounting == ["windops-external-intelligence"],
        "Only the External Intelligence Service mounts the provider's key",
    )
    since = f"--since={int(time.monotonic() - started) + 30}s"
    logs = kubectl(
        "logs", "deployment/windops-external-intelligence", since, namespace=APPLICATION
    ).stdout.lower()
    check(
        "research request sent" in logs
        and "storm and gale" not in logs
        and "gearbox oil cooler" not in logs,
        "Its logs record each request by its size, never its query",
    )


def clean_up() -> None:
    value(
        "; ".join(
            [
                "ALTER TABLE extractions DISABLE TRIGGER extractions_never_change",
                "ALTER TABLE research DISABLE TRIGGER research_never_changes",
                "DELETE FROM extractions WHERE extracted_by LIKE 'research-check-%'",
                "DELETE FROM research WHERE requested_by LIKE 'research-check-%'",
                "DELETE FROM research_policies WHERE tenant_id = 'knowledge-check'",
                "DELETE FROM idempotency_keys WHERE user_id LIKE 'research-check-%'",
                "ALTER TABLE research ENABLE TRIGGER research_never_changes",
                "ALTER TABLE extractions ENABLE TRIGGER extractions_never_change",
                "SELECT 1",
            ]
        )
    )


def main() -> int:
    started = time.monotonic()
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        # A Tenant Administrator sets the policy, and may not research (SRS 3.2): an engineer does.
        for tenant, user, role in (
            (DEMO, ENGINEER, "reliability-engineer"),
            (TENANT, ENGINEER, "reliability-engineer"),
            (TENANT, ADMIN, "tenant-administrator"),
            (OTHER, STRANGER, "reliability-engineer"),
        ):
            bootstrap(tenant, user, role)
        with (
            port_forward("deployment/windops-external-intelligence", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            caller = Caller(
                external_intelligence_pb2_grpc.ExternalIntelligenceServiceStub(channel), key
            )
            found = check_demo(caller)
            check_policy(caller)
        check_database(found)
        check_isolation(started)
    except (
        RuntimeError,
        OSError,
        grpc.RpcError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Research check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
