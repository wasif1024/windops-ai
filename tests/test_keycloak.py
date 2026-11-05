"""Check Keycloak's manifests and seeded settings against SRS section 9."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from identity_seed import (
    ADMIN_REQUIRED_ACTIONS,
    TENANTS,
    admin_client_settings,
    audience_mapper,
    client_settings,
    demo_email,
    missing_demo_passwords,
    realm_settings,
    strong_password,
    tenant_mapper,
)
from local_secrets import LOCAL_SECRETS, postgres_secrets
from provision_postgres import ADMIN_KEY, PLATFORM_DATABASES, SERVICE_NAMESPACES, provisioning_sql
from windops_auth import LEVEL_MFA, LEVEL_PASSWORD

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "infrastructure" / "security" / "keycloak"
DOCKERFILE = ROOT / "infrastructure" / "docker" / "keycloak" / "Dockerfile"


@pytest.fixture(scope="module")
def documents() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["kubectl", "kustomize", str(STACK)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def container(documents: list[dict[str, Any]]) -> dict[str, Any]:
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    result: dict[str, Any] = deployment["spec"]["template"]["spec"]["containers"][0]
    return result


def test_keycloak_is_in_the_srs_namespace(documents: list[dict[str, Any]]) -> None:
    """SRS 11.2."""
    for item in documents:
        assert item["metadata"]["namespace"] == "windops-system"


def test_keycloak_is_not_exposed_and_hides_its_management_port(
    documents: list[dict[str, Any]],
) -> None:
    """DEP-007."""
    service = next(item for item in documents if item["kind"] == "Service")
    assert service["spec"]["type"] == "ClusterIP"
    assert [port["port"] for port in service["spec"]["ports"]] == [8080]


def test_the_base_image_is_pinned() -> None:
    assert "@sha256:" in DOCKERFILE.read_text()


def test_keycloak_is_probed_and_limited(documents: list[dict[str, Any]]) -> None:
    item = container(documents)
    assert {"startupProbe", "readinessProbe", "livenessProbe"} <= set(item)
    assert "memory" in item["resources"]["limits"]


def test_keycloak_meets_the_restricted_pod_security_profile(
    documents: list[dict[str, Any]],
) -> None:
    deployment = next(item for item in documents if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    context = pod["containers"][0]["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["readOnlyRootFilesystem"] is True
    assert context["capabilities"]["drop"] == ["ALL"]


def test_every_credential_comes_from_a_secret(documents: list[dict[str, Any]]) -> None:
    """DEP-003."""
    env = {item["name"]: item for item in container(documents)["env"]}
    for name in ("KC_BOOTSTRAP_ADMIN_PASSWORD", "KC_DB_USERNAME", "KC_DB_PASSWORD"):
        assert "secretKeyRef" in env[name]["valueFrom"]


def test_keycloak_has_its_own_database_and_login() -> None:
    """ARC-003, and not a service, so it gets no cache or event login."""
    assert "keycloak" in PLATFORM_DATABASES
    assert "keycloak" not in SERVICE_NAMESPACES
    sql = provisioning_sql({"keycloak": "valid-password"})
    assert "CREATE DATABASE windops_keycloak OWNER windops_keycloak" in sql
    assert "REVOKE ALL ON DATABASE windops_keycloak FROM PUBLIC;" in sql


def test_database_credentials_stay_in_its_namespace() -> None:
    passwords = {ADMIN_KEY: "a", "keycloak": "k", **dict.fromkeys(SERVICE_NAMESPACES, "s")}
    secret: Any = next(
        item
        for item in postgres_secrets(passwords)
        if item["metadata"]["name"] == "windops-keycloak-database"  # type: ignore[index]
    )
    assert secret["metadata"]["namespace"] == "windops-system"
    assert secret["stringData"]["database"] == "windops_keycloak"


def test_admin_password_reaches_only_its_namespace() -> None:
    secret = next(item for item in LOCAL_SECRETS if item.name == "windops-keycloak-admin")
    assert secret.namespaces == ("windops-system",)


def test_access_tokens_are_short_lived_and_refresh_tokens_rotate() -> None:
    """SEC-001."""
    realm = realm_settings()
    assert realm["accessTokenLifespan"] <= 300
    assert realm["revokeRefreshToken"] is True
    assert realm["refreshTokenMaxReuse"] == 0


def test_the_realm_enforces_a_password_policy_and_lockout() -> None:
    """SEC-004."""
    realm = realm_settings()
    for rule in ("length(12)", "upperCase", "lowerCase", "digits", "specialChars", "notUsername"):
        assert rule in realm["passwordPolicy"]
    assert realm["bruteForceProtected"] is True
    assert realm["failureFactor"] <= 5


def test_nobody_can_create_their_own_account() -> None:
    assert realm_settings()["registrationAllowed"] is False


def test_tenants_are_organizations_in_one_realm() -> None:
    """SEC-005."""
    assert realm_settings()["organizationsEnabled"] is True


def test_the_browser_client_cannot_send_passwords_or_skip_pkce() -> None:
    client = client_settings()
    assert client["publicClient"] is True
    assert client["directAccessGrantsEnabled"] is False
    assert client["implicitFlowEnabled"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert "*" not in client["redirectUris"]


def test_the_tenant_claim_comes_from_the_organization() -> None:
    """SEC-005: it must be one value, in the access token."""
    config = tenant_mapper()["config"]
    assert tenant_mapper()["protocolMapper"] == "oidc-organization-membership-mapper"
    assert config["claim.name"] == "tenant_id"
    assert config["multivalued"] == "false"
    assert config["access.token.claim"] == "true"


def test_the_seed_makes_a_tenant_part_of_every_login() -> None:
    """Keycloak adds a tenant only with this scope, which is optional by default (SEC-005)."""
    source = (ROOT / "scripts" / "identity_seed.py").read_text()
    assert 'ORGANIZATION_SCOPE = "organization"' in source
    assert "default-client-scopes/" in source
    assert "ensure_organization_scope(api, " in source


def test_the_administrators_client_requires_a_second_factor() -> None:
    """SEC-002."""
    assert admin_client_settings()["attributes"]["minimum.acr.value"] == LEVEL_MFA
    assert admin_client_settings()["clientId"] != client_settings()["clientId"]


def test_the_browser_client_does_not_require_it() -> None:
    assert "minimum.acr.value" not in client_settings()["attributes"]


def test_administrators_must_set_up_both_factors() -> None:
    """SEC-002."""
    assert set(ADMIN_REQUIRED_ACTIONS) == {"CONFIGURE_TOTP", "CONFIGURE_RECOVERY_AUTHN_CODES"}


def test_codes_cannot_be_reused() -> None:
    assert realm_settings()["otpPolicyCodeReusable"] is False


def test_tokens_name_their_level() -> None:
    """SEC-003: the names services compare against."""
    levels = json.loads(realm_settings()["attributes"]["acr.loa.map"])
    assert levels == {LEVEL_PASSWORD: 1, LEVEL_MFA: 2}


def test_the_login_flow_keeps_keycloak_and_services_on_one_time_limit() -> None:
    source = (ROOT / "scripts" / "identity_seed.py").read_text()
    assert "add_level(api, second, 2, MFA_LEVEL_SECONDS)" in source
    assert "from windops_auth import" in source


def test_tokens_name_their_audience() -> None:
    """SEC-008."""
    assert audience_mapper()["config"]["included.custom.audience"] == "windops-api"


@pytest.mark.parametrize("attempt", range(20))
def test_generated_passwords_always_meet_the_policy(attempt: int) -> None:
    password = strong_password()
    assert len(password) >= 12
    assert any(item.isupper() for item in password)
    assert any(item.islower() for item in password)
    assert any(item.isdigit() for item in password)
    assert any(not item.isalnum() for item in password)


def test_demo_passwords_are_stable_once_made() -> None:
    first = missing_demo_passwords({})
    assert set(TENANTS) <= set(first)
    # Each tenant's operator and administrator, and the platform administrator.
    assert len(first) == 2 * len(TENANTS) + 1
    assert missing_demo_passwords(first) == first


def test_only_a_missing_demo_password_is_made() -> None:
    alias = next(iter(TENANTS))
    result = missing_demo_passwords({alias: "kept-value"})
    assert result[alias] == "kept-value"
    assert len(set(result.values())) == 2 * len(TENANTS) + 1


def test_demo_passwords_are_not_written_to_a_file() -> None:
    source = (ROOT / "scripts" / "identity_seed.py").read_text()
    assert "secrets.local.identity" not in source
    assert "DEMO_USERS_RESOURCE" in source


def test_demo_addresses_cannot_reach_real_mail() -> None:
    assert all(demo_email(alias).endswith(".example") for alias in TENANTS)
