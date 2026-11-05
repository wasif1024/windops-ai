#!/usr/bin/env python3
"""Prove the gateway serves only HTTPS, and exposes only Keycloak's sign-in pages (API-001).

Run `make gateway`, `make gateway-routes` and `make identity-seed` first. It needs local port 9443
free, because Keycloak's pages and redirects name https://localhost:9443.
"""

import ssl
import subprocess
import sys
import time
import warnings

import httpx
from gateway_routes import admin_key, apply_route, load_routes, port_forward
from identity_seed import PUBLIC_URL, REALM, keycloak_public
from tls_client import authority_file

CONTEXT = "windops"
SYSTEM = "windops-system"
GATEWAY_DEPLOYMENT = "deployment/windops-gateway"
DISCOVERY = "/auth/realms/windops/.well-known/openid-configuration"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def refuses_old_tls() -> bool:
    """A client that offers only TLS 1.1 must not get a connection."""
    context = ssl.create_default_context(cafile=authority_file())
    with warnings.catch_warnings():
        # Python warns that these versions are old, which is the point of trying them.
        warnings.simplefilter("ignore", DeprecationWarning)
        context.minimum_version = ssl.TLSVersion.TLSv1
        context.maximum_version = ssl.TLSVersion.TLSv1_1
    try:
        httpx.get(f"{PUBLIC_URL}/realms/{REALM}", verify=context, timeout=10)
    except (httpx.ConnectError, ssl.SSLError):
        return True
    return False


def keycloak_address() -> str:
    return subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "get", "service", "windops-keycloak",
            f"--namespace={SYSTEM}", "--output=jsonpath={.spec.clusterIP}",
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


def check_upstream_is_verified(public: str) -> None:
    """Reach Keycloak at an address its certificate does not name. A checking gateway must fail.

    The probe route takes only requests with its own header, so no real request reaches it.
    """
    route = {
        **load_routes()["keycloak"],
        "uris": [DISCOVERY],
        "vars": [["http_x_edge_probe", "==", "1"]],
        "priority": 10,
    }
    route["upstream"] = {**route["upstream"], "nodes": {f"{keycloak_address()}:8443": 1}}
    route["upstream"].pop("checks", None)
    key = admin_key()
    with port_forward(GATEWAY_DEPLOYMENT, SYSTEM, 9180) as admin_port:
        admin = f"http://127.0.0.1:{admin_port}"
        apply_route(admin, key, "edge-probe", route)
        try:
            time.sleep(2)
            url = f"{public.removesuffix('/auth')}{DISCOVERY}"
            code = httpx.get(url, headers={"X-Edge-Probe": "1"}, timeout=10).status_code
            check(
                code == 502,
                f"The gateway refuses Keycloak's certificate at an unnamed address ({code})",
            )
            normal = httpx.get(url, timeout=10).status_code
            check(normal == 200, "and the normal route, by its name, still works")
        finally:
            httpx.delete(
                f"{admin}/apisix/admin/routes/edge-probe", headers={"X-API-KEY": key}, timeout=10
            )


def main() -> int:
    try:
        ports = subprocess.run(
            [
                "kubectl", "--context", CONTEXT, "get", "service", "windops-gateway",
                f"--namespace={SYSTEM}", "--output=jsonpath={.spec.ports[*].port}",
            ],
            capture_output=True, text=True, check=True,
        ).stdout.split()  # fmt: skip
        check(ports == ["443"], "The gateway's Service offers HTTPS only")
        with keycloak_public() as public:
            discovery = httpx.get(
                f"{public}/realms/{REALM}/.well-known/openid-configuration", timeout=10
            )
            check(
                discovery.status_code == 200, "Keycloak's sign-in pages answer through the gateway"
            )
            check(
                discovery.json()["issuer"] == f"{PUBLIC_URL}/realms/{REALM}",
                f"Every token's issuer is the one fixed address: {PUBLIC_URL}/realms/{REALM}",
            )
            for path in (
                "/admin/master/console/",
                f"/admin/realms/{REALM}/users",
                "/realms/master/protocol/openid-connect/auth",
                "/realms/master/.well-known/openid-configuration",
            ):
                code = httpx.get(f"{public}{path}", timeout=10).status_code
                check(code == 404, f"{path} is not reachable from outside the cluster ({code})")
            check(refuses_old_tls(), "A client offering only TLS 1.0 or 1.1 is refused")
            check_upstream_is_verified(public)
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Edge check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
