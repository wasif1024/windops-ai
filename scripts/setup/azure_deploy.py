#!/usr/bin/env python3
"""Create, pause and delete WindOps AI's Azure resources, from the Bicep (M4A.6).

    make azure-plan      # what would change, creating nothing (COMPUTE=1 adds AKS)
    make azure-up        # the data group, then the compute group
    make azure-down      # delete the compute group and stop PostgreSQL: data stays
    make azure-destroy   # delete both groups and the budget

docs/azure-plan.md sizes and prices what this creates.

It signs in with your Azure CLI session (az login), never a stored credential. Every value comes
from .env: AZURE_SUBSCRIPTION_ID, AZURE_ALERT_EMAIL and the optional AZURE_* settings beside them.
`up`, `down` and `destroy` show what they will do and ask before doing it.

Two certificates are made once and kept in Key Vault:

- the internal CA, which cert-manager issues every in-cluster certificate from, so App Gateway
  can trust APISIX's certificate across rebuilt clusters;
- App Gateway's HTTPS certificate, self-signed by Key Vault for the gateway's Azure DNS name, so
  a browser warns until a domain and a public CA replace it.
"""

import argparse
import base64
import datetime
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from scripts.lib import settings
from scripts.lib.tls_client import trust_public_roots

REPOSITORY = Path(__file__).resolve().parents[2]
TEMPLATE = REPOSITORY / "infrastructure" / "azure" / "bicep" / "main.bicep"
# Outputs such as client IDs: not secrets, but they name your subscription, so git ignores them.
OUTPUTS = REPOSITORY / ".azure"
# Key Vault secret names, not secrets.
CA_SECRET = "windops-internal-ca"  # noqa: S105
GATEWAY_CERTIFICATE = "windops-gateway-tls"
PROVIDERS = (
    "Microsoft.ContainerService",
    "Microsoft.Compute",
    "Microsoft.Network",
    "Microsoft.DBforPostgreSQL",
    "Microsoft.KeyVault",
    "Microsoft.ContainerRegistry",
    "Microsoft.ServiceBus",
    "Microsoft.ManagedIdentity",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.Monitor",
    "Microsoft.Dashboard",
    "Microsoft.AlertsManagement",
    "Microsoft.Security",
    "Microsoft.Consumption",
)
# Azure Resource Manager's states for a deployment that has not finished.
RUNNING = {"Accepted", "Running", "Canceling"}
COST = "Compute costs about $0.98 an hour while it runs; the data group about $20 a month."
# Key Vault's data-plane roles take a few minutes to reach a new vault.
ROLE_WAIT_SECONDS, ROLE_ATTEMPTS = 15, 20


class AzureError(RuntimeError):
    pass


def az(*arguments: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["az", *arguments, "--only-show-errors"], capture_output=capture, text=True, check=False
    )
    if result.returncode != 0:
        raise AzureError((result.stderr or "").strip() or f"az {arguments[0]} failed")
    return (result.stdout or "").strip()


def az_json(*arguments: str) -> Any:
    text = az(*arguments, "--output", "json")
    return json.loads(text) if text else None


# ------------------------------------------------------------------------------------ settings


def public_ip() -> str:
    """This machine's public IPv4 address, over HTTPS with verified certificates."""
    import ssl

    context = ssl.create_default_context(cafile=trust_public_roots())
    with urllib.request.urlopen("https://api.ipify.org", timeout=10, context=context) as answer:
        body: bytes = answer.read()
    return body.decode().strip()


def ip_ranges(value: str) -> list[str]:
    """AZURE_OPERATOR_IP_RANGES, comma separated, each an IPv4 address or CIDR range."""
    ranges = [item.strip() for item in value.split(",") if item.strip()]
    for item in ranges:
        network = ipaddress.ip_network(item, strict=False)
        if network.version != 4:
            raise ValueError(f"Not IPv4, which Azure's firewalls need: {item}")
    return ranges


def deployment_settings() -> dict[str, Any]:
    values = settings.load()
    email = values.get("AZURE_ALERT_EMAIL", "")
    if "@" not in email:
        raise AzureError("Set AZURE_ALERT_EMAIL in .env: budget and alert emails go there")
    return {
        "subscription": settings.get("AZURE_SUBSCRIPTION_ID"),
        "location": values.get("AZURE_LOCATION") or "swedencentral",
        "environment": values.get("AZURE_ENVIRONMENT") or "staging",
        "budget": int(values.get("AZURE_BUDGET_USD") or 100),
        "email": email,
        "ranges": merged_ranges(values.get("AZURE_OPERATOR_IP_RANGES", ""), public_ip()),
        "configured_ranges": ip_ranges(values.get("AZURE_OPERATOR_IP_RANGES", "")),
        "operator": values.get("AZURE_OPERATOR_OBJECT_ID", ""),
        "pipeline": values.get("AZURE_PIPELINE_PRINCIPAL_ID", ""),
    }


def merged_ranges(configured: str, current: str) -> list[str]:
    """The configured ranges, and the address this runs from, so a pipeline agent can reach in."""
    ranges = ip_ranges(configured)
    if not any(ipaddress.ip_address(current) in ipaddress.ip_network(r) for r in ranges):
        ranges.append(current)
    return ip_ranges(",".join(ranges))


def operator_object_id(config: dict[str, Any]) -> str:
    """The person the deployment is for. A pipeline signs in as itself, so it is set there."""
    if config["operator"]:
        return str(config["operator"])
    signed_in = az_json("account", "show", "--query", "user")
    if signed_in.get("type") != "user":
        raise AzureError("Signed in as a service principal: set AZURE_OPERATOR_OBJECT_ID")
    return az("ad", "signed-in-user", "show", "--query", "id", "--output", "tsv")


def parameters(config: dict[str, Any], operator: str, **extra: Any) -> list[str]:
    """The template's parameters, each as name=value, with JSON for arrays and booleans."""
    values: dict[str, Any] = {
        "location": config["location"],
        "environment": config["environment"],
        "operatorIpRanges": config["ranges"],
        "operatorObjectId": operator,
        "pipelinePrincipalId": config.get("pipeline", ""),
        "alertEmail": config["email"],
        "budgetAmount": config["budget"],
        **extra,
    }
    out = []
    for name, value in values.items():
        text = value if isinstance(value, str) else json.dumps(value)
        out.append(f"{name}={text}")
    return out


def deploy(config: dict[str, Any], operator: str, what_if: bool, **extra: Any) -> dict[str, Any]:
    command = "what-if" if what_if else "create"
    arguments = [
        "deployment", "sub", command,
        "--subscription", config["subscription"],
        "--location", config["location"],
        "--name", f"windops-{config['environment']}",
        "--template-file", str(TEMPLATE),
        "--parameters", *parameters(config, operator, **extra),
    ]  # fmt: skip
    if what_if:
        az(*arguments, "--result-format", "ResourceIdOnly", capture=False)
        return {}
    result = az_json(*arguments)
    return {name: item["value"] for name, item in result["properties"]["outputs"].items()}


def unregistered_providers(subscription: str) -> list[str]:
    states = az_json(
        "provider", "list", "--subscription", subscription,
        "--query", "[].{name:namespace, state:registrationState}",
    )  # fmt: skip
    registered = {item["name"].lower() for item in states if item["state"] == "Registered"}
    return [name for name in PROVIDERS if name.lower() not in registered]


def register_providers(subscription: str) -> None:
    """Registering lets the subscription create a kind of resource. It is free, and creates none."""
    for name in unregistered_providers(subscription):
        print(f"Registering {name}")
        az("provider", "register", "--namespace", name, "--subscription", subscription, "--wait")


def confirm(question: str, expected: str = "yes") -> None:
    answer = input(f"{question} Type {expected} to go on: ").strip()
    if answer != expected:
        raise AzureError("Stopped: nothing was changed")


# ---------------------------------------------------------------------------------- certificates


def make_ca(environment: str, now: datetime.datetime | None = None) -> str:
    """A new internal CA, as one PEM holding its certificate and then its key."""
    now = now or datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"WindOps internal CA ({environment})")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    return (
        certificate.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    ).decode()


def certificate_der_base64(pem: str) -> str:
    """The certificate in a PEM, as App Gateway's trusted root takes it: base64 of its DER."""
    certificate = x509.load_pem_x509_certificate(pem.encode())
    return base64.b64encode(certificate.public_bytes(serialization.Encoding.DER)).decode()


def with_role_wait(action: Any) -> Any:
    """Retry while a just-granted Key Vault role is still reaching the vault."""
    for attempt in range(ROLE_ATTEMPTS):
        try:
            return action()
        except AzureError as error:
            if "Forbidden" not in str(error) or attempt == ROLE_ATTEMPTS - 1:
                raise
            time.sleep(ROLE_WAIT_SECONDS)
    return None


def secret_exists(vault: str, name: str) -> bool:
    found = az_json(
        "keyvault", "secret", "list", "--vault-name", vault, "--query", f"[?name=='{name}']"
    )
    return bool(found)


def ensure_ca(vault: str, environment: str) -> str:
    """The internal CA's certificate, as base64 DER, made and stored the first time."""
    if not with_role_wait(lambda: secret_exists(vault, CA_SECRET)):
        print("Making the internal CA, kept in Key Vault")
        pem = make_ca(environment)
        az(
            "keyvault", "secret", "set", "--vault-name", vault, "--name", CA_SECRET,
            "--content-type", "application/x-pem-file", "--value", pem, "--output", "none",
        )  # fmt: skip
    pem = az("keyvault", "secret", "show", "--vault-name", vault, "--name", CA_SECRET,
             "--query", "value", "--output", "tsv")  # fmt: skip
    return certificate_der_base64(pem)


def gateway_fqdn(vault: str, location: str, environment: str) -> str:
    """The gateway's Azure DNS name, from the same suffix as the vault (edge.bicep)."""
    suffix = vault.rsplit("-", 1)[-1]
    return f"windops-{environment}-{suffix}.{location}.cloudapp.azure.com"


def certificate_policy(fqdn: str) -> dict[str, Any]:
    return {
        "issuerParameters": {"name": "Self"},
        "keyProperties": {"exportable": True, "keyType": "EC", "curve": "P-256", "reuseKey": False},
        "secretProperties": {"contentType": "application/x-pkcs12"},
        "x509CertificateProperties": {
            "subject": f"CN={fqdn}",
            "subjectAlternativeNames": {"dnsNames": [fqdn]},
            "validityInMonths": 12,
            "keyUsage": ["digitalSignature", "keyAgreement"],
            "ekus": ["1.3.6.1.5.5.7.3.1"],
        },
        "lifetimeActions": [
            {"trigger": {"daysBeforeExpiry": 30}, "action": {"actionType": "AutoRenew"}}
        ],
    }


def ensure_gateway_certificate(vault: str, fqdn: str) -> str:
    """App Gateway's HTTPS certificate, by its versionless secret URI, so renewals apply."""
    existing = az_json("keyvault", "certificate", "list", "--vault-name", vault,
                       "--query", f"[?name=='{GATEWAY_CERTIFICATE}']")  # fmt: skip
    if not existing:
        print(f"Making App Gateway's certificate for {fqdn}")
        policy = json.dumps(certificate_policy(fqdn))
        az("keyvault", "certificate", "create", "--vault-name", vault,
           "--name", GATEWAY_CERTIFICATE, "--policy", policy, "--output", "none")  # fmt: skip
    return f"https://{vault}.vault.azure.net/secrets/{GATEWAY_CERTIFICATE}"


# --------------------------------------------------------------------------------------- commands


def recover_deleted_vault(config: dict[str, Any]) -> None:
    """Purge protection keeps a destroyed vault for 90 days, so take it back, not a new one."""
    prefix = f"kv-windops-{config['environment']}-"
    deleted = az_json("keyvault", "list-deleted", "--subscription", config["subscription"],
                      "--resource-type", "vault", "--query", "[].name") or []  # fmt: skip
    for name in deleted:
        if name.startswith(prefix):
            print(f"Recovering Key Vault {name}, kept since the last destroy")
            az("keyvault", "recover", "--name", name, "--subscription", config["subscription"])


def save_outputs(environment: str, outputs: dict[str, Any]) -> Path:
    OUTPUTS.mkdir(exist_ok=True)
    (OUTPUTS / ".gitignore").write_text("*\n")
    path = OUTPUTS / f"{environment}-outputs.json"
    path.write_text(json.dumps(outputs, indent=2) + "\n")
    return path


def plan(config: dict[str, Any], compute: bool) -> int:
    operator = operator_object_id(config)
    missing = unregistered_providers(config["subscription"])
    if missing:
        print("Not registered yet, so `up` registers them first, which is free:")
        for name in missing:
            print(f"  {name}")
    # Two kinds of change show as unsupported: those named after an identity made in the same run.
    deploy(config, operator, what_if=True, deployCompute=compute)
    print(COST if compute else "The data group alone costs about $20 a month.")
    return 0


def up(config: dict[str, Any], assume_yes: bool) -> int:
    print(
        f"This creates WindOps AI in {config['location']}, subscription {config['subscription']}."
    )
    print(COST)
    print(f"Allowed to the cluster API, Key Vault and Service Bus: {', '.join(config['ranges'])}")
    if not assume_yes:
        confirm("Create it?")
    operator = operator_object_id(config)
    register_providers(config["subscription"])
    recover_deleted_vault(config)
    print("Creating the data resource group")
    outputs = deploy(config, operator, what_if=False, deployCompute=False)
    vault = outputs["keyVaultName"]
    trusted_root = ensure_ca(vault, config["environment"])
    certificate = ensure_gateway_certificate(
        vault, gateway_fqdn(vault, config["location"], config["environment"])
    )
    print("Creating the compute resource group: AKS takes about 10 minutes")
    outputs = deploy(
        config, operator, what_if=False, deployCompute=True,
        gatewayCertificateUri=certificate, gatewayTrustedRootCertificate=trusted_root,
    )  # fmt: skip
    path = save_outputs(config["environment"], outputs)
    context = use_cluster(config, outputs)
    print(f"Done. Outputs in {path.relative_to(REPOSITORY)}; kubectl context {context}.")
    print(f"Gateway: https://{outputs['gatewayFqdn']}. Run `make azure-down` when finished.")
    return 0


def down(config: dict[str, Any], assume_yes: bool) -> int:
    environment = config["environment"]
    compute = f"rg-windops-{environment}-compute"
    print(f"This deletes {compute} (AKS and App Gateway) and stops PostgreSQL. Data stays.")
    if not assume_yes:
        confirm("Delete the compute?")
    subscription = config["subscription"]
    stop_deployment(config)
    if az("group", "exists", "--name", compute, "--subscription", subscription) == "true":
        print(f"Deleting {compute}: about 10 minutes")
        az("group", "delete", "--name", compute, "--subscription", subscription, "--yes")
    data = f"rg-windops-{environment}-data"
    if az("group", "exists", "--name", data, "--subscription", subscription) != "true":
        # A run rejected or cancelled before it deployed: nothing to stop, and nothing to create.
        print(f"{data} does not exist: nothing is running.")
        return 0
    print("Removing the cluster's and this machine's addresses from the firewalls")
    # Only the configured ranges stay: an agent's address is needed only while it deploys.
    kept = {**config, "ranges": config["configured_ranges"] or config["ranges"]}
    deploy(kept, operator_object_id(config), what_if=False, deployCompute=False)
    servers = az_json(
        "postgres", "flexible-server", "list", "--resource-group", data,
        "--subscription", subscription, "--query", "[].{n:name, s:state}",
    )  # fmt: skip
    for server in servers:
        if server["s"] == "Ready":
            print(f"Stopping {server['n']}. Azure starts it again after 7 days")
            az("postgres", "flexible-server", "stop", "--resource-group", data,
               "--name", server["n"], "--subscription", subscription)  # fmt: skip
    print("Done: about $20 a month until `make azure-up`.")
    return 0


def stop_deployment(config: dict[str, Any], wait_seconds: int = 900) -> None:
    """Cancel a deployment still running in Azure, as one does after its pipeline job is cancelled.

    Cancelling a job stops only the agent: Azure Resource Manager carries on creating resources.
    Deleting the compute group under it would race with it, so it is stopped first.
    """
    name, subscription = f"windops-{config['environment']}", config["subscription"]

    def state() -> str:
        return az(
            "deployment", "sub", "list", "--subscription", subscription,
            "--query", f"[?name=='{name}'].properties.provisioningState | [0]", "--output", "tsv",
        )  # fmt: skip

    if state() not in RUNNING - {"Canceling"}:
        return
    print(f"Cancelling the deployment {name}, which is still running in Azure")
    az("deployment", "sub", "cancel", "--name", name, "--subscription", subscription)
    deadline = time.monotonic() + wait_seconds
    while state() in RUNNING:
        if time.monotonic() > deadline:
            raise AzureError(f"{name} is still running after {wait_seconds} seconds")
        time.sleep(15)


def deployed_outputs(config: dict[str, Any]) -> dict[str, Any]:
    """The last deployment's outputs, from Azure, so any machine or pipeline job can read them."""
    found = az_json(
        "deployment", "sub", "show", "--subscription", config["subscription"],
        "--name", f"windops-{config['environment']}", "--query", "properties.outputs",
    )  # fmt: skip
    return {name: item["value"] for name, item in (found or {}).items()}


def use_cluster(config: dict[str, Any], outputs: dict[str, Any]) -> str:
    """A kubectl context for the cluster, signed in through kubelogin with the CLI's session."""
    context = f"windops-aks-{config['environment']}"
    az(
        "aks", "get-credentials", "--subscription", config["subscription"],
        "--resource-group", outputs["computeResourceGroup"], "--name", outputs["clusterName"],
        "--context", context, "--overwrite-existing",
    )  # fmt: skip
    if shutil.which("kubelogin") is None:
        raise AzureError("kubectl needs kubelogin for Entra sign-in: run az aks install-cli")
    subprocess.run(["kubelogin", "convert-kubeconfig", "--login", "azurecli"], check=True)
    return context


def prove(config: dict[str, Any]) -> int:
    """What the pipeline checks once the compute is up: the nodes, and events on Service Bus."""
    outputs = deployed_outputs(config)
    if not outputs.get("clusterName"):
        raise AzureError("No cluster in the last deployment: run `make azure-up` first")
    context = use_cluster(config, outputs)
    nodes = subprocess.run(
        ["kubectl", "--context", context, "get", "nodes", "--no-headers"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    ready = [line for line in nodes.stdout.splitlines() if " Ready " in f" {line} "]
    if nodes.returncode != 0 or len(ready) < 2:
        raise AzureError(f"The cluster's two nodes are not Ready: {nodes.stderr or nodes.stdout}")
    print(f"PASS {len(ready)} AKS nodes are Ready")
    # The same five-scenario contract NATS passes, on the deployed namespace (M4A.3).
    check = subprocess.run(
        [sys.executable, "-m", "scripts.checks.data.servicebus"],
        env={**os.environ, "WINDOPS_SERVICEBUS_NAMESPACE": outputs["serviceBusNamespace"]},
        cwd=REPOSITORY, check=False,
    )  # fmt: skip
    if check.returncode != 0:
        raise AzureError("Events on the deployed Service Bus failed their contract")
    return 0


def destroy(config: dict[str, Any], confirmed: str = "") -> int:
    environment, subscription = config["environment"], config["subscription"]
    groups = [f"rg-windops-{environment}-compute", f"rg-windops-{environment}-data"]
    print(
        f"This deletes {' and '.join(groups)}, with every database, image, secret and log in them,"
    )
    print("and the budget. Key Vault keeps the vault for 90 days, and `up` takes it back.")
    expected = f"destroy {environment}"
    if confirmed != environment:
        confirm("Delete everything?", expected=expected)
    for group in groups:
        if az("group", "exists", "--name", group, "--subscription", subscription) == "true":
            print(f"Deleting {group}")
            az("group", "delete", "--name", group, "--subscription", subscription, "--yes")
    az("consumption", "budget", "delete", "--budget-name", f"budget-windops-{environment}",
       "--subscription", subscription)  # fmt: skip
    print("Done. Defender for Containers stays on for the subscription, at no cost with no nodes.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="show what would change, creating nothing")
    plan_parser.add_argument("--compute", action="store_true", help="include the compute group")
    for name in ("up", "down"):
        commands.add_parser(name).add_argument("--yes", action="store_true", help="do not ask")
    commands.add_parser("prove", help="check the deployed nodes and Service Bus")
    commands.add_parser("destroy").add_argument(
        "--confirm", default="", metavar="ENVIRONMENT", help="the environment's name, to not ask"
    )
    arguments = parser.parse_args(argv)
    try:
        config = deployment_settings()
        az("account", "show", "--subscription", config["subscription"], "--output", "none")
        if arguments.command == "plan":
            return plan(config, arguments.compute)
        if arguments.command == "up":
            return up(config, arguments.yes)
        if arguments.command == "down":
            return down(config, arguments.yes)
        if arguments.command == "prove":
            return prove(config)
        return destroy(config, arguments.confirm)
    except (AzureError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
