"""Network policies: default-deny everywhere, only the calls made, and only named endpoints out.

K8S-001, EGR-001 and ADR 0047.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.generate.network_policies import (
    COMPONENT,
    EXTERNAL,
    MIGRATION,
    NAME,
    NAMESPACE_LABEL,
    NAMESPACES,
    OUTPUT,
    SETTING,
    all_workloads,
    generate,
    settings,
)

ROOT = Path(__file__).resolve().parent.parent
OVERLAYS = ROOT / "infrastructure" / "kubernetes" / "overlays"


@pytest.fixture(scope="module")
def policies() -> list[dict[str, Any]]:
    return [
        d
        for namespace in NAMESPACES
        for d in yaml.safe_load_all((OUTPUT / f"{namespace}.yaml").read_text())
        if d
    ]


def cilium(policies: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (p["metadata"]["namespace"], p["spec"]["endpointSelector"]["matchLabels"][NAME]): p
        for p in policies
        if p["kind"] == "CiliumNetworkPolicy"
        and NAME in p["spec"]["endpointSelector"].get("matchLabels", {})
    }


def test_the_policies_are_up_to_date() -> None:
    """When this fails, run: make network-policies"""
    for path, text in generate().items():
        assert (OUTPUT / path).read_text() == text, path


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_each_namespace_denies_all_ingress_and_egress(
    policies: list[dict[str, Any]], namespace: str
) -> None:
    deny = [
        p for p in policies
        if p["kind"] == "NetworkPolicy" and p["metadata"]["namespace"] == namespace
    ]  # fmt: skip
    assert len(deny) == 1
    spec = deny[0]["spec"]
    assert spec == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}


def test_every_workload_has_its_own_policy(policies: list[dict[str, Any]]) -> None:
    covered = cilium(policies)
    for workload in all_workloads():
        assert (workload.namespace, workload.name) in covered, workload.name


def test_every_call_allowed_out_is_allowed_in_at_the_other_end(
    policies: list[dict[str, Any]],
) -> None:
    covered = cilium(policies)
    for (namespace, name), policy in covered.items():
        for rule in policy["spec"].get("egress", []):
            for target in rule.get("toEndpoints", []):
                labels = target.get("matchLabels", {})
                if NAME not in labels:
                    continue
                other = covered[(labels[NAMESPACE_LABEL], labels[NAME])]
                port = rule["toPorts"][0]["ports"][0]["port"]
                assert any(
                    {NAME: name, NAMESPACE_LABEL: namespace}
                    in [s.get("matchLabels") for s in back.get("fromEndpoints", [])]
                    and port in [p["port"] for p in back["toPorts"][0]["ports"]]
                    for back in other["spec"].get("ingress", [])
                ), f"{name} may call {labels[NAME]}:{port}, which does not accept it"


def test_migration_jobs_reach_postgresql_and_it_lets_them_in(
    policies: list[dict[str, Any]],
) -> None:
    postgres = cilium(policies)[("windops-data", "windops-postgres")]
    admitted = [s["matchLabels"] for r in postgres["spec"]["ingress"] for s in r["fromEndpoints"]]
    jobs = [p for p in policies if p["metadata"]["name"] == "windops-migrations"]
    assert jobs
    for job in jobs:
        namespace = job["metadata"]["namespace"]
        assert {COMPONENT: MIGRATION, NAMESPACE_LABEL: namespace} in admitted, namespace
        (rule,) = job["spec"]["egress"]
        assert rule["toEndpoints"][0]["matchLabels"][NAME] == "windops-postgres"


def test_only_the_named_endpoints_are_reachable_outside_the_cluster(
    policies: list[dict[str, Any]],
) -> None:
    """EGR-001: no address ranges and no `world`, only DNS names, and only where declared."""
    reached: dict[str, set[str]] = {}
    for policy in policies:
        spec = policy.get("spec", {})
        for rule in spec.get("egress", []):
            assert "toCIDR" not in rule and "toCIDRSet" not in rule
            assert not set(rule.get("toEntities", [])) & {"world", "all"}, policy["metadata"]
            if "toFQDNs" in rule:
                name = spec["endpointSelector"]["matchLabels"][NAME]
                reached.setdefault(name, set()).update(
                    f.get("matchName") or f["matchPattern"] for f in rule["toFQDNs"]
                )
    declared = {
        name: {n for e in endpoints for n in (*e.names, *e.patterns)}
        for name, endpoints in EXTERNAL.items()
    }
    assert reached == declared


def test_only_the_gateway_accepts_calls_from_outside(policies: list[dict[str, Any]]) -> None:
    open_to_world = {
        p["metadata"]["name"]
        for p in policies
        for rule in p.get("spec", {}).get("ingress", [])
        if "world" in rule.get("fromEntities", [])
    }
    assert open_to_world == {"windops-gateway"}


@pytest.mark.parametrize(
    ("workload", "host"),
    [
        ("services/external-intelligence-service", "api.tavily.com"),
        ("services/knowledge-service", "api.openai.com"),
        ("services/weather-service", "api.open-meteo.com"),
    ],
)
def test_each_named_endpoint_is_the_one_the_code_calls(workload: str, host: str) -> None:
    code = "".join(p.read_text() for p in (ROOT / workload / "src").rglob("*.py"))
    assert f"https://{host}/" in code


def test_the_libraries_default_to_the_named_endpoints() -> None:
    import anthropic
    from langsmith import utils

    assert anthropic.Anthropic(api_key="x").base_url.host == "api.anthropic.com"
    assert utils.get_api_url(None) == "https://api.smith.langchain.com"


def test_every_address_the_code_reads_has_a_default() -> None:
    """A fresh .env from .env.example reaches every service the code calls."""
    known = settings()
    read = {
        setting
        for directory in ("services", "mcp-servers", "simulator", "shared")
        for path in (ROOT / directory).rglob("src/**/*.py")
        for setting in SETTING.findall(path.read_text())
        if setting.endswith("_ADDRESS")
    }
    # Set by the workload's own manifest, not windops-settings.
    own = {"WINDOPS_SAMPLE_RESPONDER_ADDRESS"}
    assert read - own <= set(known), sorted(read - own - set(known))


def test_every_migration_job_is_labelled_so_its_policy_selects_it() -> None:
    template = ROOT / "scripts/dev/service_database_template/deploy/migrate-job.yaml.tmpl"
    jobs = [*ROOT.glob("services/*/deploy/migrate-job.yaml"), template]
    for job in jobs:
        labels = re.findall(rf"{re.escape(COMPONENT)}: (\S+)", job.read_text())
        assert labels == [MIGRATION, MIGRATION], job


@pytest.mark.parametrize("overlay", ["local", "ci", "production-example"])
def test_the_overlays_deploy_the_policies(overlay: str) -> None:
    out = subprocess.run(
        ["kubectl", "kustomize", str(OVERLAYS / overlay)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    kinds = [d["kind"] for d in yaml.safe_load_all(out) if d]
    assert kinds.count("NetworkPolicy") == len(NAMESPACES)
    assert "CiliumNetworkPolicy" in kinds
