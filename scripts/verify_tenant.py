#!/usr/bin/env python3
"""Prove tenants, branding and row-level security work on the real cluster.

Run `make identity-seed`, `make migrate`, `make services`, `make authorization-seed` and
`make tenant-seed` first. Calls are signed as the real demo users, as Web BFF will sign them, and
every decision comes from the real Authorization Service. The tenant this creates is removed at
the end.
"""

import base64
import contextlib
import re
import secrets
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
from gateway_routes import port_forward
from identity_seed import (
    ADMIN_SECRET_FILE,
    PLATFORM_ADMIN_EMAIL,
    REALM,
    admin_client,
    demo_admin_email,
    demo_email,
    keycloak_admin,
)
from provision_postgres import read_passwords
from settings import KUBE_CONTEXT
from tls_client import ClientBundle, blocking_channel, client_bundle
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.tenant.v1 import tenant_pb2 as pb
from windops.tenant.v1 import tenant_pb2_grpc
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD
from windops_grpc import VerifiedIdentity, sign_identity

CONTEXT = KUBE_CONTEXT
SYSTEM, APPLICATION, DATA = "windops-system", "windops-application", "windops-data"
DEPLOYMENT = "deployment/windops-tenant"
HOST = "windops-tenant.windops-system.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
TENANT, OTHER, PLATFORM = "north-sea", "atlas-wind", "windops-platform"
RUN = secrets.token_hex(3)
NEW_TENANT = f"tenant-check-{RUN}"
DOMAIN = f"ops-{RUN}.north-sea.example"
PNG = b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(64)
DATABASE = "windops_tenant"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, namespace: str = SYSTEM) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={namespace}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def sql(statement: str) -> subprocess.CompletedProcess[str]:
    """Run SQL as the Tenant Service's own login, which owns the tables, inside the database pod."""
    return kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={statement}",
        namespace=DATA,
    )  # fmt: skip


def signing_key() -> str:
    encoded = kubectl(
        "get", "secret", "windops-internal-identity", "--output=jsonpath={.data.signing-key}"
    ).stdout
    return base64.b64decode(encoded).decode()


class Caller:
    def __init__(self, channel: grpc.Channel, key: str) -> None:
        self.stub: Any = tenant_pb2_grpc.TenantServiceStub(channel)
        self.key = key

    def metadata(self, user: str | None, tenant: str, level: str | None = LEVEL_MFA) -> Any:
        identity = VerifiedIdentity(
            subject=user or "svc-web-bff",
            tenant_id=tenant,
            user_id=user,
            auth_level=level if user else None,
            auth_time=int(time.time()) if user and level else None,
        )
        return (("x-windops-identity", sign_identity(identity, self.key, ttl_seconds=60)),)

    def call(self, method: str, request: Any, user: str | None, tenant: str, **options: Any) -> Any:
        return getattr(self.stub, method)(
            request, metadata=self.metadata(user, tenant, **options), timeout=15
        )

    def code(self, method: str, request: Any, user: str | None, tenant: str, **options: Any) -> Any:
        try:
            self.call(method, request, user, tenant, **options)
        except grpc.RpcError as error:
            return error.code()
        return grpc.StatusCode.OK


def user_id(api: httpx.Client, email: str) -> str:
    found = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
    found.raise_for_status()
    return str(found.json()[0]["id"])


def organization(api: httpx.Client, alias: str) -> dict[str, Any] | None:
    listed = api.get(f"/{REALM}/organizations", params={"first": 0, "max": 1000}).json()
    return next((item for item in listed if item["alias"] == alias), None)


def branding_request(**changes: str) -> Any:
    values = {
        "display_name": "North Sea Wind",
        "primary_color": "#0b5394",
        "accent_color": "#38761d",
        "login_title": "Sign in to North Sea Wind",
        "login_message": f"Tenant check {RUN}",
        "time_zone": "Europe/Oslo",
        "locale": "en-GB",
        **changes,
    }
    return pb.UpdateBrandingRequest(**values)


def check_reading(caller: Caller, operator: str) -> None:
    mine = caller.call("GetBranding", pb.GetBrandingRequest(), None, TENANT)
    check(mine.branding.tenant_id == TENANT, "A service acting for a tenant reads its branding")
    check(
        caller.code("GetBranding", pb.GetBrandingRequest(target_tenant_id=OTHER), operator, TENANT)
        is grpc.StatusCode.PERMISSION_DENIED,
        "A user cannot read another tenant's branding, as the Authorization Service decides",
    )


def check_branding(caller: Caller, admin: str, operator: str, other_admin: str) -> None:
    check(
        caller.code("UpdateBranding", branding_request(), admin, TENANT, level=LEVEL_PASSWORD)
        is grpc.StatusCode.PERMISSION_DENIED,
        "Changing branding without a recent second factor is refused (SEC-003)",
    )
    check(
        caller.code("UpdateBranding", branding_request(), operator, TENANT)
        is grpc.StatusCode.PERMISSION_DENIED,
        "A Control Room Operator cannot change branding",
    )
    updated = caller.call("UpdateBranding", branding_request(), admin, TENANT)
    check(
        updated.branding.login_message == f"Tenant check {RUN}",
        "The Tenant Administrator changes the branding",
    )
    takeover = branding_request(display_name="Taken over")
    takeover.target_tenant_id = OTHER
    check(
        caller.code("UpdateBranding", takeover, admin, TENANT) is grpc.StatusCode.PERMISSION_DENIED,
        "but cannot change another tenant's (SEC-007)",
    )
    check(
        caller.code("UpdateBranding", branding_request(primary_color="#ffff99"), admin, TENANT)
        is grpc.StatusCode.INVALID_ARGUMENT,
        "A primary color that white text cannot be read on is refused",
    )
    caller.call("SetLogo", pb.SetLogoRequest(image=PNG, content_type="image/png"), admin, TENANT)
    logo = caller.call("GetLogo", pb.GetLogoRequest(), None, TENANT)
    check(logo.image == PNG, "A PNG logo is stored and served back exactly")
    svg = pb.SetLogoRequest(image=b"<svg><script/></svg>", content_type="image/svg+xml")
    check(
        caller.code("SetLogo", svg, admin, TENANT) is grpc.StatusCode.INVALID_ARGUMENT,
        "An SVG logo is refused, because it can carry scripts",
    )
    bad = pb.SetEmailTemplateRequest(template="invitation", subject="Hi {{password}}", body="x")
    check(
        caller.code("SetEmailTemplate", bad, admin, TENANT) is grpc.StatusCode.INVALID_ARGUMENT,
        "An email template cannot use a placeholder it is not given",
    )
    caller.call("SetCustomDomain", pb.SetCustomDomainRequest(domain=DOMAIN), admin, TENANT)
    check(
        caller.code("SetCustomDomain", pb.SetCustomDomainRequest(domain=DOMAIN), other_admin, OTHER)
        is grpc.StatusCode.FAILED_PRECONDITION,
        "Another tenant cannot claim the same domain",
    )


def check_lifecycle(caller: Caller, api: httpx.Client, admin: str, platform: str) -> None:
    create = pb.CreateTenantRequest(target_tenant_id=NEW_TENANT, name=f"Tenant Check {RUN}")
    check(
        caller.code("CreateTenant", create, admin, TENANT) is grpc.StatusCode.PERMISSION_DENIED,
        "A Tenant Administrator cannot create tenants",
    )
    created = caller.call("CreateTenant", create, platform, PLATFORM)
    found = organization(api, NEW_TENANT)
    check(
        created.created and found is not None and found["enabled"] is True,
        "The Platform Administrator creates a tenant, with its Keycloak organization",
    )
    listed = caller.call("ListTenants", pb.ListTenantsRequest(page_size=200), platform, PLATFORM)
    check(
        {TENANT, OTHER, NEW_TENANT} <= {t.tenant_id for t in listed.tenants},
        "The platform's staff list every tenant",
    )
    suspend = pb.SuspendTenantRequest(target_tenant_id=NEW_TENANT, reason=f"Tenant check {RUN}")
    caller.call("SuspendTenant", suspend, platform, PLATFORM)
    disabled = organization(api, NEW_TENANT)
    check(
        disabled is not None and disabled["enabled"] is False,
        "Suspending it disables its organization, so its users' tokens carry no tenant",
    )
    caller.call(
        "ReactivateTenant",
        pb.ReactivateTenantRequest(target_tenant_id=NEW_TENANT),
        platform,
        PLATFORM,
    )
    enabled = organization(api, NEW_TENANT)
    check(enabled is not None and enabled["enabled"] is True, "Reactivating it enables it again")


def check_row_level_security() -> None:
    """DAT-005, asked of PostgreSQL itself, as the service's own login, which owns the tables."""
    everyone = sql(
        "SELECT set_config('windops.all_tenants', 'on', false); SELECT count(*) FROM branding"
    )
    rows = int(everyone.stdout.strip().splitlines()[-1]) if everyone.returncode == 0 else 0
    check(rows >= 3, f"There are {rows} tenants' branding rows, for the checks below to hide")
    nobody = sql("SELECT count(*) FROM branding")
    check(
        nobody.returncode == 0 and nobody.stdout.strip() == "0",
        "With no tenant named, the service's own login sees no branding at all",
    )
    one = sql(
        "SELECT set_config('windops.tenant_id', 'north-sea', false); "
        "SELECT string_agg(DISTINCT tenant_id, ',') FROM branding"
    )
    check(
        one.returncode == 0 and one.stdout.strip().splitlines()[-1] == TENANT,
        "Naming one tenant shows only its rows, even with no WHERE in the query",
    )
    write = sql(
        "SELECT set_config('windops.tenant_id', 'north-sea', false); "
        "UPDATE branding SET login_message = 'changed' WHERE tenant_id = 'atlas-wind' RETURNING 1"
    )
    check(
        write.returncode == 0 and "1" not in write.stdout.strip().splitlines()[1:],
        "A change to another tenant's row finds nothing to change",
    )
    insert = sql(
        "SELECT set_config('windops.tenant_id', 'north-sea', false); "
        "INSERT INTO email_templates VALUES "
        "('atlas-wind', 'invitation', 's', 'b', 'check', now())"
    )
    check(
        insert.returncode != 0 and "row-level security" in insert.stderr,
        "Writing a row for another tenant is refused by PostgreSQL",
    )
    forced = sql(
        "SELECT bool_and(relforcerowsecurity) FROM pg_class "
        "WHERE relname IN ('tenants', 'branding', 'email_templates', 'custom_domains')"
    )
    check(forced.stdout.strip() == "t", "All four tables force the policy on their owner")


def check_audit(bundle: ClientBundle, key: str, admin: str) -> None:
    wanted = {"tenant.branding.updated", "tenant.logo.updated", "tenant.custom_domain.set"}
    identity = VerifiedIdentity(admin, TENANT, admin)
    metadata = (("x-windops-identity", sign_identity(identity, key, ttl_seconds=60)),)
    found: set[str] = set()
    for _ in range(30):
        with (
            port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
            blocking_channel(port, AUDIT_HOST, bundle) as channel,
        ):
            stub: Any = audit_pb2_grpc.AuditServiceStub(channel)
            records = stub.ListAuditRecords(
                audit_pb2.ListAuditRecordsRequest(page_size=500), metadata=metadata, timeout=10
            ).records
        found = {r.action for r in records if r.producer == "tenant" and r.actor_id == admin}
        if wanted <= found:
            break
        time.sleep(2)
    check(wanted <= found, f"{TENANT}'s audit chain holds the branding changes")


def cleanup(caller: Caller, api: httpx.Client, admin: str) -> None:
    with contextlib.suppress(grpc.RpcError):
        caller.call("RemoveCustomDomain", pb.RemoveCustomDomainRequest(), admin, TENANT)
    found = organization(api, NEW_TENANT)
    if found is not None:
        api.delete(f"/{REALM}/organizations/{found['id']}")
    # Made by this script from token_hex, and checked here, so it cannot carry any SQL.
    if not re.fullmatch(r"tenant-check-[0-9a-f]{6}", NEW_TENANT):
        raise RuntimeError("Unexpected tenant ID")
    match = f"tenant_id = '{NEW_TENANT}'"
    deletes = f"DELETE FROM branding WHERE {match}; DELETE FROM tenants WHERE {match}"  # noqa: S608
    sql(f"SELECT set_config('windops.all_tenants', 'on', false); {deletes}")


def run(base: str, password: str) -> None:
    status = kubectl("rollout", "status", DEPLOYMENT, "--timeout=180s")
    check(status.returncode == 0, "The Tenant Service is running")
    bundle, key = client_bundle(), signing_key()
    with admin_client(base, password) as api:
        admin = user_id(api, demo_admin_email(TENANT))
        operator = user_id(api, demo_email(TENANT))
        other_admin = user_id(api, demo_admin_email(OTHER))
        platform = user_id(api, PLATFORM_ADMIN_EMAIL)
        with (
            port_forward(DEPLOYMENT, SYSTEM, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            caller = Caller(channel, key)
            try:
                check_reading(caller, operator)
                check_branding(caller, admin, operator, other_admin)
                check_lifecycle(caller, api, admin, platform)
                check_row_level_security()
            finally:
                cleanup(caller, api, admin)
        check_audit(bundle, key, admin)


def main() -> int:
    password = read_passwords(ADMIN_SECRET_FILE)["password"]
    try:
        with keycloak_admin() as base:
            run(base, password)
    except (RuntimeError, OSError, grpc.RpcError, httpx.HTTPError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Tenant check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
