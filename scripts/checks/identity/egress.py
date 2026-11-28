#!/usr/bin/env python3
"""Prove each tenant's egress switches on the cluster (EGR-002, ADR 0048).

Run after `make services`, `make migrate` and `make authorization-seed`. In the demo tenant
north-sea, with synthetic users the Authorization Service grants roles to, it proves:

- every path is on until turned off, and a service acting for the tenant reads them;
- only a person holding mcp.configure, with a recent second factor and a reason, turns one off,
  and each change reaches the audit trail;
- another tenant is not affected;
- with embeddings off, a document search pauses and says why, as the Knowledge Service reads the
  switch from the real Tenant Service, before anything is sent;
- with forecasts off, the Weather Service fetches no new forecast.

It sends nothing to any provider: a search is made only while embeddings are off. Every switch it
turned off is turned on again at the end.
"""

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from windops.knowledge.v1 import knowledge_pb2, knowledge_pb2_grpc
from windops.tenant.v1 import tenant_pb2 as pb
from windops.tenant.v1 import tenant_pb2_grpc
from windops.weather.v1 import weather_pb2, weather_pb2_grpc
from windops_auth import LEVEL_PASSWORD
from windops_grpc.egress import EMBEDDINGS, FORECASTS, PATHS, turned_off

from scripts.checks.fleet.asset import Caller, bootstrap, check, kubectl, signing_key
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward

SYSTEM, APPLICATION, DATA = "windops-system", "windops-application", "windops-data"
TENANT, OTHER, TURBINE = "north-sea", "atlas-wind", "dogger-east-t01"
ADMIN, OPERATOR = "egress-check-admin", "egress-check-operator"
REASON = "Egress check: proving the switch"
# The services keep an answer this long (windops_grpc.egress), and a little more.
SETTLE_SECONDS = 35


def tenant_host(service: str, namespace: str) -> str:
    return f"windops-{service}.{namespace}.svc.cluster.local"


def switches(tenants: Caller, tenant: str = TENANT) -> dict[str, Any]:
    # As a service acting for the tenant: no user, so no person's grant is needed to read.
    answer = tenants.stub.GetEgress(
        pb.GetEgressRequest(), metadata=tenants.metadata("svc-weather", tenant, None), timeout=15
    )
    return {p.path: p for p in answer.paths}


def turn(tenants: Caller, path: str, enabled: bool, user: str = ADMIN, **kw: Any) -> Any:
    request = pb.SetEgressRequest(path=path, enabled=enabled, reason=REASON)
    return tenants.call("SetEgress", request, user, TENANT, **kw)


def check_switches(tenants: Caller) -> None:
    now = switches(tenants)
    check(
        [p.name for p in PATHS] == list(now) and all(p.enabled for p in now.values()),
        f"Every path is on until turned off: {', '.join(now)}",
    )
    check(
        tenants.code("SetEgress", pb.SetEgressRequest(path="claude", enabled=False, reason=REASON),
                     OPERATOR, TENANT) is grpc.StatusCode.PERMISSION_DENIED,
        "A Control Room Operator cannot turn a path off",
    )  # fmt: skip
    check(
        tenants.code("SetEgress", pb.SetEgressRequest(path="claude", enabled=False, reason=REASON),
                     ADMIN, TENANT, level=LEVEL_PASSWORD) is grpc.StatusCode.PERMISSION_DENIED,
        "Nor can a Tenant Administrator without a recent second factor (SEC-003)",
    )  # fmt: skip
    for path in (EMBEDDINGS.name, FORECASTS.name):
        changed = turn(tenants, path, False).path
        check(
            not changed.enabled and changed.reason == REASON and changed.updated_by == ADMIN,
            f"The Tenant Administrator turns {path} off, with the reason kept",
        )
    check(
        all(p.enabled for p in switches(tenants, OTHER).values()),
        "Another tenant's paths stay on",
    )


def check_knowledge(knowledge: Caller) -> None:
    """The Knowledge Service keeps the switch's answer briefly, so it may take that long."""
    deadline = time.monotonic() + SETTLE_SECONDS
    code, details = grpc.StatusCode.OK, ""
    while True:
        try:
            knowledge.call(
                "SearchPassages", knowledge_pb2.SearchPassagesRequest(query="gearbox oil"),
                OPERATOR, TENANT,
            )  # fmt: skip
        except grpc.RpcError as error:
            code, details = error.code(), error.details() or ""
        else:
            raise RuntimeError("A search ran while embeddings were off")
        if turned_off(EMBEDDINGS) in details or time.monotonic() > deadline:
            break
        time.sleep(3)
    check(
        code is grpc.StatusCode.UNAVAILABLE and turned_off(EMBEDDINGS) in details,
        f"With embeddings off, search pauses and says why: {details}",
    )


def forecasts_kept() -> int:
    found = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", "--username=windops_weather", "--dbname=windops_weather",
        f"--command=SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608  # A constant.
        "SELECT count(*) FROM forecasts",
        namespace=DATA,
    )  # fmt: skip
    return int(found.stdout.strip().splitlines()[-1])


def check_weather(weather: Caller) -> None:
    before = forecasts_kept()
    now = datetime.now(UTC)
    start, end = Timestamp(), Timestamp()
    start.FromDatetime(now)
    end.FromDatetime(now + timedelta(hours=6))
    answer = weather.call(
        "GetForecast",
        weather_pb2.GetForecastRequest(turbine_id=TURBINE, from_time=start, to_time=end),
        OPERATOR, TENANT,
    )  # fmt: skip
    check(forecasts_kept() == before, "With forecasts off, no new forecast is fetched")
    shown = f"{answer.state}" + (f": {answer.note}" if answer.note else "")
    check(
        not answer.note or turned_off(FORECASTS) in answer.note,
        f"The answer gives the kept forecast, or says why there is none ({shown})",
    )


def check_audit() -> None:
    found = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", "--username=windops_audit", "--dbname=windops_audit",
        f"--command=SELECT set_config('windops.tenant_id', '{TENANT}', false); "  # noqa: S608  # A constant.
        "SELECT action, resource_id FROM audit_records "
        "WHERE action LIKE 'tenant.egress.%' ORDER BY recorded_at DESC LIMIT 20",
        namespace=DATA,
    )  # fmt: skip
    recorded = found.stdout
    check(
        f"tenant.egress.turned_off|{TENANT}/embeddings" in recorded
        and f"tenant.egress.turned_on|{TENANT}/embeddings" in recorded,
        "Turning a path off and on again reached the audit trail",
    )


def main() -> int:
    restore: list[str] = []
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        with (
            port_forward("deployment/windops-tenant", SYSTEM, 50051) as tenant_port,
            blocking_channel(tenant_port, tenant_host("tenant", SYSTEM), bundle) as channel,
        ):
            tenants = Caller(tenant_pb2_grpc.TenantServiceStub(channel), key)
            try:
                restore = [EMBEDDINGS.name, FORECASTS.name]
                check_switches(tenants)
                with (
                    port_forward("deployment/windops-knowledge", APPLICATION, 50051) as port,
                    blocking_channel(port, tenant_host("knowledge", APPLICATION), bundle) as kc,
                ):
                    check_knowledge(Caller(knowledge_pb2_grpc.KnowledgeServiceStub(kc), key))
                with (
                    port_forward("deployment/windops-weather", APPLICATION, 50051) as port,
                    blocking_channel(port, tenant_host("weather", APPLICATION), bundle) as wc,
                ):
                    check_weather(Caller(weather_pb2_grpc.WeatherServiceStub(wc), key))
            finally:
                for path in restore:
                    turn(tenants, path, True)
            check(all(p.enabled for p in switches(tenants).values()), "Every path is on again")
        # The audit records go through the outbox and events, so they take a moment.
        time.sleep(10)
        check_audit()
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Egress check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
