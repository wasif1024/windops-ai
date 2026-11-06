#!/usr/bin/env python3
"""Attempt cross-tenant access on every data path, on the real cluster (DAT-005, DAT-008, SEC-007).

Run the seeds and `make services` first. Two layers are attacked:

1. PostgreSQL itself. In every service's database, as that service's own login, which owns the
   tables, every table with a tenant must force row-level security, and must hide or refuse other
   tenants' rows even when a query forgets its WHERE.
2. Each service's API, as a real demo administrator of one tenant, reaching for another's data.
"""

import base64
import re
import subprocess
import sys
import time
from typing import Any

import grpc
import httpx
from gateway_routes import port_forward
from identity_seed import (
    ADMIN_SECRET_FILE,
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
from windops.authorization.v1 import authorization_pb2 as authz
from windops.authorization.v1 import authorization_pb2_grpc
from windops.identity.v1 import identity_pb2, identity_pb2_grpc
from windops.tenant.v1 import tenant_pb2 as tp
from windops.tenant.v1 import tenant_pb2_grpc
from windops_auth import LEVEL_MFA
from windops_grpc import VerifiedIdentity, sign_identity

CONTEXT = KUBE_CONTEXT
SYSTEM, APPLICATION, DATA = "windops-system", "windops-application", "windops-data"
TENANT, OTHER = "north-sea", "atlas-wind"
# Every service that stores data so far. A new one must be added here.
DATABASES = ("audit", "authorization", "identity", "tenant")
# Tables with a tenant that need no policy, and why. The repository test exempts the same.
EXEMPT = {"outbox_events"}  # Only the service's relay reads it, never a request.
FORBIDDEN = {grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.NOT_FOUND}
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$|^[a-z0-9][a-z0-9-]{1,62}$")
Calls = list[tuple[str, Any]]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, namespace: str = SYSTEM) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={namespace}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def sql(service: str, statement: str) -> subprocess.CompletedProcess[str]:
    """Run SQL as a service's own login, which owns its tables, inside the database pod."""
    login = f"windops_{service}"
    return kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={login}", f"--dbname={login}", "--set=ON_ERROR_STOP=1",
        f"--command={statement}",
        namespace=DATA,
    )  # fmt: skip


def statement(template: str, **names: str) -> str:
    """Fill in table and tenant names, each checked, as SQL cannot take them as parameters."""
    for value in names.values():
        if not _NAME.match(value):
            raise ValueError(f"Not a safe name: {value!r}")
    return template.format(**names)


def last_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def as_tenant(tenant: str, query: str) -> str:
    return (
        statement("SELECT set_config('windops.tenant_id', '{tenant}', false); ", tenant=tenant)
        + query
    )


def tenant_tables(service: str) -> dict[str, tuple[bool, bool]]:
    """Every table with a tenant_id, and whether it enables and forces row-level security."""
    result = sql(
        service,
        "SELECT c.relname || ',' || c.relrowsecurity || ',' || c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid "
        "WHERE a.attname = 'tenant_id' AND c.relkind = 'r' "
        "AND c.relnamespace = 'public'::regnamespace ORDER BY c.relname",
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not read {service}'s tables: {result.stderr.strip()}")
    tables = {}
    for line in result.stdout.split():
        name, enabled, forced = line.split(",")
        # A boolean joined onto text reads "true" or "false".
        tables[name] = (enabled == "true", forced == "true")
    return tables


def check_table(service: str, table: str) -> None:
    everyone = sql(
        service,
        statement(
            "SELECT set_config('windops.all_tenants', 'on', false); "
            "SELECT count(DISTINCT tenant_id) FROM {table}",
            table=table,
        ),
    )
    tenants = int(last_line(everyone) or 0)
    nobody = sql(service, statement("SELECT count(*) FROM {table}", table=table))
    check(
        last_line(nobody) == "0",
        f"{service}.{table}: with no tenant named, none of its {tenants} tenants' rows show",
    )
    others = sql(
        service,
        as_tenant(
            TENANT,
            statement(
                "SELECT count(*) FROM {table} WHERE tenant_id <> '{t}'", table=table, t=TENANT
            ),
        ),
    )
    check(last_line(others) == "0", f"{service}.{table}: naming {TENANT} hides other tenants' rows")
    # In a transaction that is always rolled back, so nothing changes even if this failed.
    moved = sql(
        service,
        statement(
            "BEGIN; SELECT set_config('windops.tenant_id', '{t}', true); "
            "UPDATE {table} SET tenant_id = '{o}' WHERE tenant_id = '{t}'; ROLLBACK;",
            table=table,
            t=TENANT,
            o=OTHER,
        ),
    )
    mine = sql(service, as_tenant(TENANT, statement("SELECT count(*) FROM {table}", table=table)))
    refused = moved.returncode != 0 and (
        "row-level security" in moved.stderr or "append-only" in moved.stderr
    )
    note = "" if refused else f" (it holds no {TENANT} rows to try)"
    check(
        refused or last_line(mine) == "0",
        f"{service}.{table}: a row cannot be moved into another tenant{note}",
    )


def check_database(service: str) -> int:
    """Attack one database's tables, and return how many there were."""
    tables = {name: state for name, state in tenant_tables(service).items() if name not in EXEMPT}
    check(bool(tables), f"{service}: {len(tables)} tenant tables ({', '.join(tables)})")
    for table, (enabled, forced) in tables.items():
        check(enabled and forced, f"{service}.{table} enables and forces row-level security")
        check_table(service, table)
    return len(tables)


class Attacker:
    """Calls each API as one tenant's real administrator, reaching for another tenant's data."""

    def __init__(self, key: str, bundle: ClientBundle, admin: str) -> None:
        self.key, self.bundle, self.admin = key, bundle, admin
        self.attempts = 0

    def metadata(self) -> Any:
        identity = VerifiedIdentity(
            subject=self.admin,
            tenant_id=TENANT,
            user_id=self.admin,
            auth_level=LEVEL_MFA,
            auth_time=int(time.time()),
        )
        return (("x-windops-identity", sign_identity(identity, self.key, ttl_seconds=60)),)

    def refused(self, label: str, stub: Any, calls: Calls) -> None:
        for name, request in calls:
            self.attempts += 1
            code = grpc.StatusCode.OK
            try:
                getattr(stub, name)(request, metadata=self.metadata(), timeout=10)
            except grpc.RpcError as error:
                code = error.code()
            check(code in FORBIDDEN, f"{label}: {name} on {OTHER} is refused ({code.name})")

    def against(self, deployment: str, namespace: str, stub_type: Any, calls: Calls) -> None:
        host = f"{deployment}.{namespace}.svc.cluster.local"
        with (
            port_forward(f"deployment/{deployment}", namespace, 50051) as port,
            blocking_channel(port, host, self.bundle) as channel,
        ):
            self.refused(deployment.removeprefix("windops-").title(), stub_type(channel), calls)

    def check_authorization(self) -> None:
        host = "windops-authorization.windops-system.svc.cluster.local"
        with (
            port_forward("deployment/windops-authorization", SYSTEM, 50051) as port,
            blocking_channel(port, host, self.bundle) as channel,
        ):
            stub: Any = authorization_pb2_grpc.AuthorizationServiceStub(channel)
            request = authz.CheckRequest(permission="turbine.read", target_tenant_id=OTHER)
            self.attempts += 1
            check(
                not stub.Check(request, metadata=self.metadata(), timeout=10).allowed,
                f"Authorization: Check in {OTHER} is denied",
            )
            self.refused(
                "Authorization",
                stub,
                [
                    ("ListRoleGrants", authz.ListRoleGrantsRequest(target_tenant_id=OTHER)),
                    ("GrantRole", authz.GrantRoleRequest(
                        target_tenant_id=OTHER, grantee_id=self.admin,
                        role="tenant-administrator")),
                    ("SetRolePermissions", authz.SetRolePermissionsRequest(
                        target_tenant_id=OTHER, role="auditor", permissions=[])),
                ],
            )  # fmt: skip

    def check_audit(self) -> None:
        host = "windops-audit.windops-application.svc.cluster.local"
        seen: set[str] = set()
        with (
            port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
            blocking_channel(port, host, self.bundle) as channel,
        ):
            stub: Any = audit_pb2_grpc.AuditServiceStub(channel)
            token = ""
            for _ in range(200):
                page = stub.ListAuditRecords(
                    audit_pb2.ListAuditRecordsRequest(page_size=500, page_token=token),
                    metadata=self.metadata(),
                    timeout=10,
                )
                seen |= {record.event_id for record in page.records}
                token = page.next_page_token
                if not token:
                    break
        self.attempts += 1
        mine = int(last_line(sql("audit", as_tenant(TENANT, "SELECT count(*) FROM audit_records"))))
        theirs = set(
            sql("audit", as_tenant(OTHER, "SELECT event_id FROM audit_records")).stdout.split()[1:]
        )
        # The request has no tenant field, so the caller's verified tenant is the only one possible.
        check(
            len(seen) == mine and bool(theirs) and not seen & theirs,
            f"Audit: {TENANT} reads exactly its {mine} records, none of {OTHER}'s {len(theirs)}",
        )


def demo_users() -> tuple[str, str]:
    password = read_passwords(ADMIN_SECRET_FILE)["password"]
    with (
        keycloak_admin() as base,
        admin_client(base, password) as api,
    ):
        found = []
        for email in (demo_admin_email(TENANT), demo_email(OTHER)):
            response = api.get(f"/{REALM}/users", params={"email": email, "exact": "true"})
            response.raise_for_status()
            found.append(str(response.json()[0]["id"]))
    return found[0], found[1]


def main() -> int:
    try:
        tables = sum(check_database(service) for service in DATABASES)
        encoded = kubectl(
            "get", "secret", "windops-internal-identity", "--output=jsonpath={.data.signing-key}"
        ).stdout
        admin, stranger = demo_users()
        attacker = Attacker(base64.b64decode(encoded).decode(), client_bundle(), admin)
        attacker.check_authorization()
        attacker.against(
            "windops-identity",
            SYSTEM,
            identity_pb2_grpc.IdentityServiceStub,
            [
                ("ListUsers", identity_pb2.ListUsersRequest(target_tenant_id=OTHER)),
                ("GetUser", identity_pb2.GetUserRequest(member_id=stranger)),
                ("ListSessions", identity_pb2.ListSessionsRequest(member_id=stranger)),
                ("GetMfaStatus", identity_pb2.GetMfaStatusRequest(member_id=stranger)),
                ("DisableUser", identity_pb2.DisableUserRequest(member_id=stranger)),
                ("RevokeSessions", identity_pb2.RevokeSessionsRequest(member_id=stranger)),
                ("SetOrganizationEnabled", identity_pb2.SetOrganizationEnabledRequest(
                    target_tenant_id=OTHER, enabled=False)),
            ],
        )  # fmt: skip
        attacker.against(
            "windops-tenant",
            SYSTEM,
            tenant_pb2_grpc.TenantServiceStub,
            [
                ("GetTenant", tp.GetTenantRequest(target_tenant_id=OTHER)),
                ("GetBranding", tp.GetBrandingRequest(target_tenant_id=OTHER)),
                ("GetLogo", tp.GetLogoRequest(target_tenant_id=OTHER)),
                ("ListEmailTemplates", tp.ListEmailTemplatesRequest(target_tenant_id=OTHER)),
                ("UpdateBranding", tp.UpdateBrandingRequest(
                    target_tenant_id=OTHER, display_name="Taken", primary_color="#0b5394",
                    accent_color="#38761d", time_zone="UTC", locale="en-GB")),
                ("SetEmailTemplate", tp.SetEmailTemplateRequest(
                    target_tenant_id=OTHER, template="invitation", subject="x", body="y")),
                ("SuspendTenant", tp.SuspendTenantRequest(
                    target_tenant_id=OTHER, reason="Cross-tenant attempt")),
                ("ListTenants", tp.ListTenantsRequest()),
            ],
        )  # fmt: skip
        attacker.check_audit()
    except (RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"Cross-tenant check passed: {tables} tables and {attacker.attempts} API attempts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
