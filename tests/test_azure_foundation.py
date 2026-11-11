"""The Azure Bicep is made from the code's own facts, and keeps the plan's security (M4A.6)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from windops_events.servicebus import SUBSCRIPTIONS, filter_for, topics

from scripts.generate.azure_foundation import OUT, generate, iso_seconds
from scripts.setup.azure_deploy import (
    certificate_der_base64,
    gateway_fqdn,
    ip_ranges,
    make_ca,
    parameters,
)

ROOT = Path(__file__).resolve().parent.parent
BICEP = ROOT / "infrastructure" / "azure" / "bicep"
OVERLAY = ROOT / "infrastructure" / "kubernetes" / "overlays" / "azure" / "workload-identity.yaml"


def bicep_source() -> str:
    return "\n".join(path.read_text() for path in sorted(BICEP.rglob("*.bicep")))


def test_the_generated_files_are_up_to_date() -> None:
    """When this fails, run: make azure-foundation"""
    for path, text in generate().items():
        assert path.read_text() == text, path


def test_every_overlay_service_account_has_an_identity_to_federate() -> None:
    accounts = {
        (d["metadata"]["namespace"], d["metadata"]["name"])
        for d in yaml.safe_load_all(OVERLAY.read_text())
        if d and d["kind"] == "ServiceAccount"
    }
    workloads = json.loads((OUT / "workloads.json").read_text())
    federated = {(w["namespace"], w["name"]) for w in workloads}
    assert accounts <= federated
    assert ("windops-observability", "windops-otel-collector") in federated


def test_only_the_audit_consumer_may_receive_and_only_its_own_subscription() -> None:
    workloads = {w["name"]: w for w in json.loads((OUT / "workloads.json").read_text())}
    receivers = {name: w["receives"] for name, w in workloads.items() if w["receives"]}
    assert receivers == {"windops-audit": [{"topic": "windops-audit", "subscription": "audit"}]}
    # A workload that never reads the events settings gets no Service Bus role at all.
    assert not workloads["windops-web-bff"]["events"]
    assert not workloads["windops-otel-collector"]["database"]


def test_the_topology_is_the_one_the_code_creates() -> None:
    topology = json.loads((OUT / "servicebus.json").read_text())
    assert [t["name"] for t in topology["topics"]] == [name for name, _ in topics()]
    assert all(t["duplicateWindow"] == "PT2M" for t in topology["topics"])
    assert [(s["topic"], s["name"], s["filter"]) for s in topology["subscriptions"]] == [
        (s.topic, s.durable, filter_for(s.subject)) for s in SUBSCRIPTIONS
    ]


def test_durations_are_iso_8601() -> None:
    assert [iso_seconds(s) for s in (30, 120, 3600, 86400, 90061)] == [
        "PT30S", "PT2M", "PT1H", "P1D", "P1DT1H1M1S",
    ]  # fmt: skip


@pytest.mark.skipif(shutil.which("az") is None, reason="needs the Azure CLI's Bicep")
def test_the_bicep_builds_without_a_warning() -> None:
    result = subprocess.run(
        ["az", "bicep", "build", "--file", str(BICEP / "main.bicep"), "--stdout"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    problems = [
        line for line in result.stderr.splitlines()
        if ("Warning" in line or "Error" in line) and "new Bicep release" not in line
    ]  # fmt: skip
    assert result.returncode == 0 and not problems, problems
    json.loads(result.stdout)


@pytest.mark.parametrize(
    "setting",
    [
        "disableLocalAuth: true",  # Service Bus: Entra only, no shared keys
        "passwordAuth: 'Disabled'",  # PostgreSQL: Entra only
        "publicNetworkAccess: 'Disabled'",  # PostgreSQL: private access only
        "enablePurgeProtection: true",
        "enableRbacAuthorization: true",
        "adminUserEnabled: false",  # the registry
        "disableLocalAccounts: true",  # AKS: Entra and Azure RBAC only
        "mode: 'Prevention'",  # the WAF blocks, not only logs
        "networkDataplane: 'cilium'",
        "workloadIdentity: {",
        "minimumTlsVersion: '1.2'",
        "tier: 'Free'",  # the plan's Free-tier control plane
        "dailyQuotaGb: 2",  # the log cap
    ],
)
def test_the_bicep_keeps_the_plans_security_and_cost_settings(setting: str) -> None:
    assert setting in bicep_source()


def test_the_bicep_holds_no_secret_or_subscription() -> None:
    """The only IDs written down are Azure's built-in roles; the rest come from parameters."""
    source = bicep_source()
    guids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", source))
    assert guids <= ROLE_GUIDS
    assert not re.search(r"(?i)\b(password|secret|key)\s*:\s*'", source)


# Built-in role definitions are public IDs, the same in every tenant.
ROLE_GUIDS = {
    "69a216fc-b8fb-44d8-bc22-1f3c2cd27a39",
    "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0",
    "090c5cfd-751d-490a-894a-3ce6f1109419",
    "4633458b-17de-408a-b874-0445c86b69e6",
    "b86a8fe4-44ce-4948-aee5-eccb2c155cd7",
    "a4417e6f-fecd-4de8-b567-7b0420556985",
    "4d97b98b-1d4f-4787-a291-c67834d212e7",
    "b0d8363b-8ddd-447d-831f-62ca05bff136",
    "43d0d8ad-25c7-4714-9337-8ba259a9fe05",
    "22926164-76b3-42b3-bc55-97df8dab3e41",
    "b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b",
    "7f951dda-4ed3-4680-a7ca-43fe172d538d",
}


def test_operator_ranges_must_be_ipv4() -> None:
    assert ip_ranges(" 203.0.113.7, 198.51.100.0/24 ") == ["203.0.113.7", "198.51.100.0/24"]
    with pytest.raises(ValueError):
        ip_ranges("2001:db8::1")


def test_parameters_pass_arrays_and_booleans_as_json() -> None:
    config = {
        "location": "swedencentral", "environment": "dev", "ranges": ["203.0.113.7"],
        "email": "operator@example.com", "budget": 100,
    }  # fmt: skip
    found = parameters(config, "operator-id", deployCompute=True)
    assert 'operatorIpRanges=["203.0.113.7"]' in found
    assert "deployCompute=true" in found
    assert "budgetAmount=100" in found


def test_the_internal_ca_is_a_ca_and_app_gateway_can_read_it() -> None:
    pem = make_ca("dev")
    certificate = x509.load_pem_x509_certificate(pem.encode())
    assert certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert "PRIVATE KEY" in pem
    der = certificate_der_base64(pem)
    assert x509.load_der_x509_certificate(__import__("base64").b64decode(der)) == certificate


def test_the_gateway_name_matches_edge_bicep() -> None:
    assert gateway_fqdn("kv-windops-dev-abcde", "swedencentral", "dev") == (
        "windops-dev-abcde.swedencentral.cloudapp.azure.com"
    )
    assert "domainNameLabel: 'windops-${environment}-${suffix}'" in bicep_source()


# ------------------------------------------------------------------------- Azure DevOps (M4A.6)

PIPELINE = ROOT / "infrastructure" / "azure" / "pipelines" / "azure-pipelines.yml"


def pipeline_text() -> str:
    return PIPELINE.read_text() + (PIPELINE.parent / "templates" / "setup.yml").read_text()


def test_the_pipeline_validates_deploys_after_approval_proves_and_removes_compute() -> None:
    text = PIPELINE.read_text()
    order = [
        text.index(f"- stage: {name}") for name in ("Validate", "Deploy", "Prove", "RemoveCompute")
    ]
    assert order == sorted(order)
    # Deploying and destroying run as deployment jobs on the environment with the approval.
    assert text.count("environment: windops-staging") == 2
    assert "azure_deploy up --yes" in text and "azure_deploy prove" in text
    # The compute goes when Prove or Deploy failed and when the run was cancelled, but not when
    # Validate stopped the run. always() is what lets a stage run after a cancel.
    assert (
        "condition: and(always(), in(dependencies.Deploy.result, "
        "'Succeeded', 'SucceededWithIssues', 'Failed', 'Canceled'))"
    ) in text
    assert "azure_deploy down --yes" in text
    assert "destroy --confirm staging" in text
    assert "pr: none" in text  # GitHub Actions checks pull requests


def test_the_pipeline_signs_in_with_federation_and_holds_no_secret() -> None:
    text = pipeline_text()
    assert "azureSubscription: $(serviceConnection)" in text
    assert not re.search(r"(?i)(password|client_?secret|AZURE_CLIENT_SECRET|\bkey\s*:)", text)
    assert "AZURE_ENVIRONMENT\n    value: staging" in text
    # Tools are pinned: uv to a version, and the packages to the lock.
    assert "uv sync --frozen" in text and "UV_VERSION" in text


def test_the_pipeline_may_grant_only_the_roles_the_bicep_uses() -> None:
    from scripts.setup.azure_pipeline import RBAC_ADMINISTRATOR, assignment_condition, bicep_roles

    roles = bicep_roles()
    assert set(roles) == ROLE_GUIDS
    condition = assignment_condition(roles)
    owner, user_access_administrator = (
        "8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9",
    )  # fmt: skip
    assert owner not in condition and user_access_administrator not in condition
    assert RBAC_ADMINISTRATOR not in condition
    assert condition.count("ForAnyOfAnyValues:GuidEquals") == 2  # writes and deletes


def test_the_pipeline_trusts_only_a_service_connection_in_its_own_tenant() -> None:
    from scripts.setup.azure_deploy import AzureError
    from scripts.setup.azure_pipeline import federation

    tenant, other = "00000000-1111-2222-3333-444444444444", "11111111-2222-3333-4444-555555555555"
    connection = "22222222-3333-4444-5555-666666666666/33333333-4444-5555-6666-777777777777"
    entra = f"https://login.microsoftonline.com/{tenant}/v2.0"
    subject = f"/eid1/c/pub/t/AAAAAAAAAAAAAAAAAAAAAA/a/BBBBBBBBBBBBBBBBBBBBBB/sc/{connection}"
    assert federation(f" {entra} ", subject, tenant) == (entra, subject)
    vstoken = f"https://vstoken.dev.azure.com/{other}/"
    older = federation(vstoken, "sc://org/windops-ai/windops-staging", tenant)
    assert older[0] == vstoken.rstrip("/")
    for issuer, wrong in (
        (f"https://login.microsoftonline.com/{other}/v2.0", subject),  # another tenant
        ("https://token.actions.githubusercontent.com", subject),  # not Azure DevOps
        (entra, "repo:wasif1024/windops-ai:ref:refs/heads/main"),  # not a service connection
        (entra, subject + "/extra"),
    ):
        with pytest.raises(AzureError):
            federation(issuer, wrong, tenant)


class FakeAz:
    """Answers az commands from a table, and records them, so down runs without Azure."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers, self.calls = answers, list[str]()

    def __call__(self, *arguments: str, capture: bool = True) -> str:
        command = " ".join(arguments[:3])
        self.calls.append(command)
        replies = self.answers.get(command, [""])
        return replies.pop(0) if len(replies) > 1 else replies[0]


def test_down_creates_nothing_when_nothing_was_deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.setup import azure_deploy

    fake = FakeAz({"group exists --name": ["false"], "deployment sub list": [""]})
    monkeypatch.setattr(azure_deploy, "az", fake)
    monkeypatch.setattr(azure_deploy, "deploy", lambda *_, **__: pytest.fail("deployed"))
    config = {"environment": "staging", "subscription": "s", "ranges": [], "configured_ranges": []}
    assert azure_deploy.down(config, assume_yes=True) == 0
    assert "group delete --name" not in fake.calls and "deployment sub cancel" not in fake.calls


def test_down_first_cancels_a_deployment_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.setup import azure_deploy

    fake = FakeAz({"deployment sub list": ["Running", "Canceling", "Canceled"]})
    monkeypatch.setattr(azure_deploy, "az", fake)
    monkeypatch.setattr("time.sleep", lambda _: None)
    azure_deploy.stop_deployment({"environment": "staging", "subscription": "s"})
    assert fake.calls.count("deployment sub cancel") == 1
    assert fake.calls.count("deployment sub list") == 3


def test_staging_is_an_azure_environment_and_test_is_not() -> None:
    from windops_events.backend import ON_AZURE as EVENTS_ON_AZURE
    from windops_migrations.connection import ON_AZURE as DATABASE_ON_AZURE

    assert EVENTS_ON_AZURE == DATABASE_ON_AZURE == {"dev", "staging", "live"}
    assert "@allowed(['dev', 'staging', 'live'])" in bicep_source()


def test_a_run_allows_its_own_address_once() -> None:
    from scripts.setup.azure_deploy import merged_ranges

    assert merged_ranges("203.0.113.7", "198.51.100.9") == ["203.0.113.7", "198.51.100.9"]
    assert merged_ranges("198.51.100.0/24", "198.51.100.9") == ["198.51.100.0/24"]
    assert merged_ranges("", "198.51.100.9") == ["198.51.100.9"]
