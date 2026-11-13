#!/usr/bin/env python3
"""Prove telemetry ingestion on the real cluster: device credentials, the gateway route, storage.

Run `make migrate`, `make services`, `make gateway-routes`, the seeds, `make asset-seed` and
`make telemetry-devices` first. Needs local port 9443 free. Samples go through the gateway as a
device sends them, with the demo turbines' own tokens. The check's samples say their source is
`telemetry-check`, so they are never mistaken for the simulator's. Safe to repeat.
"""

import base64
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.telemetry.v1 import telemetry_pb2 as pb
from windops.telemetry.v1 import telemetry_pb2_grpc
from windops_auth import LEVEL_PASSWORD
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.simulator import Simulator

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import PUBLIC_URL, keycloak_public
from scripts.setup.telemetry_devices import DEVICE_STORE

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-telemetry.windops-application.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
DATABASE = "windops_telemetry"
INGEST = PUBLIC_URL.removesuffix("/auth") + "/ingest/v1/samples"
TENANT, TURBINE = "north-sea", "dogger-east-t01"
ADMIN = "telemetry-check-admin"
SOURCE = "telemetry-check"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, namespace: str = APPLICATION) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, f"--namespace={namespace}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def sql(statement: str) -> str:
    """Run SQL as the Telemetry Service's own login, which owns the tables."""
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={statement}",
        namespace=DATA,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def as_tenant(tenant: str, query: str) -> str:
    return sql(f"SELECT set_config('windops.tenant_id', '{tenant}', false); {query}")


def device_token(tenant: str, turbine: str) -> str:
    encoded = kubectl(
        "get", "secret", DEVICE_STORE, f"--output=jsonpath={{.data.{tenant}\\.{turbine}}}"
    ).stdout
    if not encoded:
        raise RuntimeError(
            f"No token for {tenant}/{turbine} in {DEVICE_STORE}: run make telemetry-devices"
        )
    return base64.b64decode(encoded).decode()


def samples(
    turbine_id: str, count: int, sequence_from: int, source: str = SOURCE
) -> list[dict[str, Any]]:
    """Fresh samples, timed now, so they land in today's partition."""
    spec = next(spec for spec in DEMO_TURBINES if spec.turbine_id == turbine_id)
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=count)
    simulator = Simulator(int(time.time()), (spec,), start)
    out = []
    for offset in range(count):
        sample = dict(simulator.step(1.0)[0].as_dict())
        sample["sequence"], sample["source"] = sequence_from + offset, source
        out.append(sample)
    return out


def send(token: str | None, batch: list[Any]) -> httpx.Response:
    return send_to("samples", token, batch)


def send_to(route: str, token: str | None, batch: list[Any]) -> httpx.Response:
    """A batch through the gateway's device route, as a device would: samples, or alarms."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = INGEST.rsplit("/", 1)[0] + f"/{route}"
    return httpx.post(url, json={route: batch}, headers=headers, timeout=15)


def check_ingestion(token: str, other_tenant_token: str) -> int:
    sequence = int(time.time())
    good = send(token, samples(TURBINE, 3, sequence))
    check(
        good.status_code == 200 and (good.json()["accepted"], good.json()["rejected"]) == (3, []),
        "A demo turbine's device sends samples through the gateway, and all 3 are stored",
    )
    wrong = send(token, [*samples("dogger-east-t02", 1, sequence), "{cut off"])
    check(
        [item["reason"] for item in wrong.json()["rejected"]] == ["wrong_turbine", "not_json"],
        "Another turbine's sample and a broken one are refused, each with its reason (FR-TEL-005)",
    )
    stranger = send(other_tenant_token, samples(TURBINE, 1, sequence))
    check(
        stranger.json()["rejected"][0]["reason"] == "wrong_tenant",
        "atlas-wind's device cannot send north-sea's samples",
    )
    nobody = send(None, samples(TURBINE, 1, sequence))
    check(
        nobody.status_code == 401 and nobody.json()["code"] == "bad_credential",
        "A request with no device credential is refused",
    )
    return sequence


def check_storage(sequence: int) -> None:
    today = f"operational_samples_{datetime.now(UTC):%Y%m%d}"
    rows = f"FROM operational_samples WHERE source = '{SOURCE}' AND sequence >= {sequence}"
    check(sql(f"SELECT count(*) {rows}") == "0", "Without a tenant, no sample is visible")
    check(
        as_tenant(TENANT, f"SELECT count(*) {rows} AND turbine_id = '{TURBINE}'") == "3",
        "north-sea sees its 3 samples",
    )
    check(
        as_tenant("atlas-wind", f"SELECT count(*) {rows}") == "0",
        "atlas-wind sees none of them (DAT-005)",
    )
    where = as_tenant(TENANT, f"SELECT DISTINCT tableoid::regclass {rows}")
    check(where == today, f"They are stored in today's daily partition, {today} (ADR 0003)")
    forced = sql(
        "SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND relname IN "  # noqa: S608
        f"('operational_samples', 'operational_samples_default', '{today}', "
        "'device_credentials', 'ingest_rejections', 'idempotency_keys')"
    )
    check(forced == "6", "Row-level security is forced on every table and partition")
    times = as_tenant(
        TENANT,
        f"SELECT bool_and(ingested_at >= event_time AND quality_status = 'received') {rows}",
    )
    check(times == "t", "Each keeps its event and ingestion times apart, as received (FR-TEL-002)")
    rejected = as_tenant(
        TENANT,
        "SELECT count(*) FROM ingest_rejections WHERE reason IN ('wrong_turbine', 'not_json') "
        "AND received_at > now() - interval '5 minutes'",
    )
    check(int(rejected) >= 2, "Refused samples are kept with their reasons (FR-TEL-003)")


def check_plain_port() -> None:
    with port_forward("deployment/windops-telemetry", APPLICATION, 8080) as port:
        plain = httpx.post(f"http://127.0.0.1:{port}/ingest/v1/samples", json={"samples": []})
        health = httpx.get(f"http://127.0.0.1:{port}/health/live")
    check(
        plain.status_code == 404 and health.status_code == 200,
        "The plain port has health and metrics only, and the device route needs TLS",
    )


def calling(telemetry: Caller, method: str, request: Any, **kw: Any) -> Any:
    """Call as the check's administrator, trying again while a dependency is still waking.

    The first call after a quiet spell opens fresh connections to Asset and Authorization, which
    can outlast the service's deadline, so it answers UNAVAILABLE, as it should (SRS 7.7). Each
    try signs a fresh identity.
    """
    for attempt in range(5):
        try:
            return telemetry.call(method, request, ADMIN, TENANT, **kw)
        except grpc.RpcError as error:
            if error.code() is not grpc.StatusCode.UNAVAILABLE or attempt == 4:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")  # pragma: no cover


def check_credential_api(telemetry: Caller) -> str:
    """FR-TEL-005: an administrator makes and revokes a credential, which works only until then."""
    request = pb.CreateDeviceCredentialRequest(turbine_id=TURBINE)
    try:
        calling(telemetry, "CreateDeviceCredential", request, level=LEVEL_PASSWORD)
        refused = grpc.StatusCode.OK
    except grpc.RpcError as error:
        refused = error.code()
    check(
        refused is grpc.StatusCode.PERMISSION_DENIED,
        "Making a credential without a recent second factor is refused",
    )
    made = calling(telemetry, "CreateDeviceCredential", request)
    device = made.credential.device_id
    first = send(made.token, samples(TURBINE, 1, int(time.time()), f"{SOURCE}-credential"))
    check(first.status_code == 200, "A Tenant Administrator makes a credential, and it works")
    calling(telemetry, "RevokeDeviceCredential", pb.RevokeDeviceCredentialRequest(device_id=device))
    after = send(made.token, samples(TURBINE, 1, int(time.time()), f"{SOURCE}-credential"))
    check(after.status_code == 401, "Once revoked, the same token is refused")
    return str(device)


def check_audit(telemetry_key: str, device: str) -> None:
    bundle = client_bundle()
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, bundle) as channel,
    ):
        audit = Caller(audit_pb2_grpc.AuditServiceStub(channel), telemetry_key)
        # Asked by action, as north-sea's trail is long and is listed oldest first.
        wanted = ("telemetry.device_credential.created", "telemetry.device_credential.revoked")
        actions: set[str] = set()
        for _ in range(30):
            for action in wanted:
                request = audit_pb2.ListAuditRecordsRequest(page_size=500, action=action)
                while True:
                    page = audit.call("ListAuditRecords", request, ADMIN, TENANT)
                    actions |= {
                        r.action
                        for r in page.records
                        if r.producer == "telemetry" and r.resource_id == device
                    }
                    if not page.next_page_token:
                        break
                    request.page_token = page.next_page_token
            if len(actions) == len(wanted):
                break
            time.sleep(2)
    check(
        actions == {"telemetry.device_credential.created", "telemetry.device_credential.revoked"},
        "Making and revoking it reached the tamper-evident audit trail",
    )


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        token, atlas = device_token(TENANT, TURBINE), device_token("atlas-wind", "atlas-ridge-t01")
        with keycloak_public():
            sequence = check_ingestion(token, atlas)
            with (
                port_forward("deployment/windops-telemetry", APPLICATION, 50051) as port,
                blocking_channel(port, HOST, bundle) as channel,
            ):
                telemetry = Caller(telemetry_pb2_grpc.TelemetryServiceStub(channel), key)
                device = check_credential_api(telemetry)
        check_storage(sequence)
        check_plain_port()
        check_audit(key, device)
    except (
        RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Telemetry check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
