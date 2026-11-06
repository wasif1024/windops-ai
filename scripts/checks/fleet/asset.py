#!/usr/bin/env python3
"""Prove the Asset Service on the real cluster: isolation, scoped reads, changes and their events.

Run `make migrate`, `make services`, the seeds and `make asset-seed` first. Calls are signed as
Web BFF signs them, and every decision comes from the real Authorization Service. Its changes go
to its own tenant, `asset-check`, so the demo tenants stay as they are, and repeating it is safe.
"""

import asyncio
import base64
import subprocess
import sys
import time
import uuid
from typing import Any

import grpc
from windops.asset.v1 import asset_pb2 as pb
from windops.asset.v1 import asset_pb2_grpc
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.authorization.v1 import authorization_pb2 as authz
from windops.authorization.v1 import authorization_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_events import parse_event
from windops_grpc import VerifiedIdentity, sign_identity

from scripts.checks.data.audit import nats_client
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.nats_access import SECRETS_FILE as NATS_SECRETS
from scripts.setup.provision_postgres import read_passwords

SYSTEM, APPLICATION, DATA = "windops-system", "windops-application", "windops-data"
HOST = "windops-asset.windops-application.svc.cluster.local"
AUTHORIZATION_HOST = "windops-authorization.windops-system.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
DATABASE = "windops_asset"
# Its own tenant, so the demo tenants are not changed. Repeating the check finds what it made.
CHECK_TENANT = "asset-check"
ADMIN, OPERATOR, STRANGER = "asset-check-admin", "asset-check-operator", "asset-check-nobody"
FARM_A, FARM_B = "check-farm-a", "check-farm-b"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, namespace: str = SYSTEM) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, f"--namespace={namespace}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def sql(statement: str) -> str:
    """Run SQL as the Asset Service's own login, which owns the tables."""
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={statement}",
        namespace=DATA,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    return result.stdout.strip().splitlines()[-1]


def signing_key() -> str:
    encoded = kubectl(
        "get", "secret", "windops-internal-identity", "--output=jsonpath={.data.signing-key}"
    ).stdout
    return base64.b64decode(encoded).decode()


def bootstrap(tenant: str, user: str, role: str) -> None:
    result = kubectl(
        "exec", "deployment/windops-authorization", "--", "python", "-m",
        "windops_authorization.bootstrap", "--tenant", tenant, "--user", user, "--role", role,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"Could not grant {role}: {result.stderr.strip()}")


class Caller:
    def __init__(self, stub: Any, key: str) -> None:
        self.stub, self.key = stub, key

    def metadata(self, user: str, tenant: str, level: str | None = LEVEL_MFA) -> Any:
        identity = VerifiedIdentity(
            subject=user,
            tenant_id=tenant,
            user_id=user,
            auth_level=level,
            auth_time=int(time.time()) if level else None,
        )
        return (("x-windops-identity", sign_identity(identity, self.key, ttl_seconds=60)),)

    def call(
        self, method: str, request: Any, user: str, tenant: str = CHECK_TENANT, **kw: Any
    ) -> Any:
        return getattr(self.stub, method)(
            request, metadata=self.metadata(user, tenant, **kw), timeout=15
        )

    def code(
        self, method: str, request: Any, user: str, tenant: str = CHECK_TENANT, **kw: Any
    ) -> Any:
        try:
            self.call(method, request, user, tenant, **kw)
        except grpc.RpcError as error:
            return error.code()
        return grpc.StatusCode.OK


def check_isolation() -> None:
    """DAT-005: forced row-level security, even for the tables' owner."""
    check(sql("SELECT count(*) FROM turbines") == "0", "Without a tenant, no turbine is visible")
    north = sql(
        "SELECT set_config('windops.tenant_id', 'north-sea', false); SELECT count(*) FROM turbines"
    )
    atlas = sql(
        "SELECT set_config('windops.tenant_id', 'atlas-wind', false); SELECT count(*) FROM turbines"
    )
    check(
        (north, atlas) == ("8", "5"), "Each demo tenant sees only its own seeded turbines (8 and 5)"
    )


def check_demo_reads(assets: Caller, operator_of: dict[str, str]) -> None:
    north = assets.call(
        "ListTurbines", pb.ListTurbinesRequest(), operator_of["north-sea"], "north-sea"
    )
    check(len(north.turbines) == 8, "north-sea's operator lists its 8 synthetic turbines")
    got = assets.call(
        "GetTurbine",
        pb.GetTurbineRequest(turbine_id="dogger-east-t01"),
        operator_of["north-sea"],
        "north-sea",
    )
    kinds = {part.kind for part in got.components}
    check(
        got.turbine.model.manufacturer == "WindOps Synthetic"
        and {"gearbox", "gearbox_bearing", "main_bearing"} <= kinds,
        "A seeded turbine is marked synthetic, with its gearbox and bearings (FR-AST-002, SIM-006)",
    )
    hidden = assets.code(
        "GetTurbine",
        pb.GetTurbineRequest(turbine_id="dogger-east-t01"),
        operator_of["atlas-wind"],
        "atlas-wind",
    )
    check(
        hidden is grpc.StatusCode.NOT_FOUND, "atlas-wind's operator cannot see north-sea's turbine"
    )


def make_check_assets(assets: Caller) -> None:
    for farm in (FARM_A, FARM_B):
        assets.call(
            "CreateWindFarm",
            pb.CreateWindFarmRequest(
                wind_farm_id=farm, name=farm, country="GB", time_zone="Europe/London",
                location=pb.Location(latitude=54.0, longitude=1.0), offshore=True,
            ),
            ADMIN,
        )  # fmt: skip
        assets.call(
            "CreateTurbine",
            pb.CreateTurbineRequest(
                turbine_id=f"{farm}-t01", wind_farm_id=farm, name=f"{farm} T01",
                location=pb.Location(latitude=54.0, longitude=1.0),
                model=pb.TurbineModel(manufacturer="WindOps Synthetic", model="check"),
            ),
            ADMIN,
        )  # fmt: skip
    check(True, "A Tenant Administrator creates wind farms and turbines, or finds them from before")


def check_scoped_read(assets: Caller, authorization: Any, key: str) -> None:
    """RBAC-002: a role for one wind farm shows that wind farm only, decided by Authorization."""
    caller = Caller(authorization, key)
    grants = caller.call(
        "ListRoleGrants", authz.ListRoleGrantsRequest(grantee_id=OPERATOR), ADMIN
    ).grants
    if not any(g.role == "control-room-operator" and not g.revoked_at.seconds for g in grants):
        caller.call(
            "GrantRole",
            authz.GrantRoleRequest(
                grantee_id=OPERATOR, role="control-room-operator",
                scope=authz.Scope(wind_farm_id=FARM_A), idempotency_key=uuid.uuid4().hex,
            ),
            ADMIN,
        )  # fmt: skip
    listed = assets.call("ListTurbines", pb.ListTurbinesRequest(), OPERATOR)
    check(
        [item.turbine_id for item in listed.turbines] == [f"{FARM_A}-t01"],
        "An operator granted one wind farm lists only its turbines",
    )
    other = assets.code("GetTurbine", pb.GetTurbineRequest(turbine_id=f"{FARM_B}-t01"), OPERATOR)
    check(
        other is grpc.StatusCode.PERMISSION_DENIED, "and is refused the other wind farm's turbine"
    )
    nobody = assets.call("ListTurbines", pb.ListTurbinesRequest(), STRANGER)
    check(not nobody.turbines, "Someone with no role sees no turbines")


def check_changes(assets: Caller) -> tuple[str, int]:
    turbine_id = f"{FARM_A}-t01"
    without = assets.code(
        "SetTurbineStatus",
        pb.SetTurbineStatusRequest(turbine_id=turbine_id, status="maintenance", reason="Check"),
        ADMIN,
        level=LEVEL_PASSWORD,
    )
    check(
        without is grpc.StatusCode.PERMISSION_DENIED,
        "A change without a recent second factor is refused",
    )
    refused = assets.code(
        "SetTurbineStatus",
        pb.SetTurbineStatusRequest(turbine_id=turbine_id, status="maintenance", reason="Check"),
        OPERATOR,
    )
    check(refused is grpc.StatusCode.PERMISSION_DENIED, "An operator cannot change a turbine")
    before = assets.call(
        "GetTurbine", pb.GetTurbineRequest(turbine_id=turbine_id), ADMIN
    ).turbine.version
    status = "maintenance" if before % 2 else "operating"
    changed = assets.call(
        "SetTurbineStatus",
        pb.SetTurbineStatusRequest(
            turbine_id=turbine_id,
            status=status,
            reason="Asset check" if status != "operating" else "",
        ),
        ADMIN,
    ).turbine
    check(
        changed.status == status and changed.version == before + 1,
        f"A status change raises the turbine's version, from {before} to {changed.version}",
    )
    return turbine_id, int(changed.version)


async def evidence_announced(turbine_id: str, version: int) -> bool:
    passwords = read_passwords(NATS_SECRETS)
    with port_forward("pod/windops-nats-0", DATA, 4222) as port:
        admin = await nats_client(port, passwords, None)
        try:
            reader = await admin.jetstream().pull_subscribe(
                "evidence.changed", stream="WINDOPS_EVENTS"
            )
            found = False
            for _ in range(15):
                try:
                    for message in await reader.fetch(500, timeout=2):
                        event = parse_event(message.subject, message.data)
                        payload: Any = event.payload
                        found = found or (
                            event.tenant_id == CHECK_TENANT
                            and payload.record_id == turbine_id
                            and payload.version == str(version)
                        )
                        await message.ack()
                except TimeoutError:
                    if found:
                        break
            await reader.unsubscribe()
            return found
        finally:
            await admin.close()


def check_audit(bundle: ClientBundle, key: str, turbine_id: str) -> None:
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, bundle) as channel,
    ):
        audit = Caller(audit_pb2_grpc.AuditServiceStub(channel), key)
        found = False
        for _ in range(30):
            records = audit.call(
                "ListAuditRecords", audit_pb2.ListAuditRecordsRequest(page_size=500), ADMIN
            ).records
            found = any(
                r.producer == "asset"
                and r.resource_id == turbine_id
                and r.action.startswith("asset.turbine.status.")
                for r in records
            )
            if found:
                break
            time.sleep(2)
    check(found, "The change reached the tamper-evident audit trail")


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(CHECK_TENANT, ADMIN, "tenant-administrator")
        check_isolation()
        # A reader in each demo tenant, as a Control Room Operator, signed as Web BFF signs users.
        operator_of = {
            tenant: f"asset-check-reader-{tenant}" for tenant in ("north-sea", "atlas-wind")
        }
        for tenant, user in operator_of.items():
            bootstrap(tenant, user, "control-room-operator")
        with (
            port_forward("deployment/windops-asset", APPLICATION, 50051) as asset_port,
            blocking_channel(asset_port, HOST, bundle) as asset_channel,
            port_forward("deployment/windops-authorization", SYSTEM, 50051) as authz_port,
            blocking_channel(authz_port, AUTHORIZATION_HOST, bundle) as authz_channel,
        ):
            assets = Caller(asset_pb2_grpc.AssetServiceStub(asset_channel), key)
            check_demo_reads(assets, operator_of)
            make_check_assets(assets)
            check_scoped_read(
                assets, authorization_pb2_grpc.AuthorizationServiceStub(authz_channel), key
            )
            turbine_id, version = check_changes(assets)
        check(
            asyncio.run(evidence_announced(turbine_id, version)),
            "and was announced as evidence.changed (SRS 8.5)",
        )
        check_audit(bundle, key, turbine_id)
    except (RuntimeError, OSError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Asset check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
