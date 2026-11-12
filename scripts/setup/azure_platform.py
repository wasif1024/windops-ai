#!/usr/bin/env python3
"""Deploy WindOps AI's services onto the AKS cluster, after `make azure-up` (M4A.6, ADR 0006).

    make azure-platform

It deploys the services that prove the platform's Azure services, with no stored password:

| Proven | By |
| --- | --- |
| Workload ID to PostgreSQL | Each service's Entra login and database, and its migrations |
| Workload ID to Service Bus | The services publish and receive their events there |
| Key Vault | Every Secret a service reads comes from it, through the Secrets Store CSI driver |
| Mutual TLS | cert-manager issues each service's certificate from the CA kept in Key Vault |
| Azure Monitor | Traces reach Application Insights, and managed Prometheus scrapes the pods |

The other services need Keycloak, Redis or the gateway's etcd in the cluster, which the full
deployment brings in Phase 15, so they are left out here. In order, it:

1. starts PostgreSQL if `make azure-down` stopped it, as `make azure-up` does too;
2. builds the services' images and pushes them to the registry, tagged with the commit;
3. writes each service's database settings, and the internal signing key, to Key Vault;
4. installs cert-manager, with the internal CA from Key Vault as its issuer;
5. deploys the Collector for Application Insights, and managed Prometheus's scrape job;
6. runs a Job, as PostgreSQL's Entra administrator, that gives each service its database and login;
7. runs each service's migrations, signed in as the service, then deploys the services.

Every step is safe to repeat.
"""

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml

from scripts.lib import settings
from scripts.setup import run_migrations
from scripts.setup.azure_deploy import CA_SECRET, AzureError, az, az_json, start_postgres

REPOSITORY = Path(__file__).resolve().parents[2]
OVERLAY = REPOSITORY / "infrastructure" / "kubernetes" / "overlays" / "azure"
MONITORING = REPOSITORY / "infrastructure" / "monitoring" / "azure"
SECURITY = REPOSITORY / "infrastructure" / "security"
# The services this deploys: each needs only PostgreSQL, Service Bus, Key Vault and mutual TLS.
WORKLOADS = (
    "windops-asset",
    "windops-audit",
    "windops-authorization",
    "windops-tenant",
    "windops-sample-caller",
    "windops-sample-responder",
)
SETTINGS_NAMESPACES = ("windops-system", "windops-application")
OBSERVABILITY = "windops-observability"
DATA = "windops-data"
POSTGRES_ADMIN = "windops-postgres-admin"
SIGNING_KEY = "windops-internal-identity--signing-key"  # a name, not a secret
PLACEHOLDER = re.compile(r"\$\{(AZURE_[A-Z0-9_]+)\}")
CSI_DRIVER = "secrets-store.csi.k8s.io"
# Where the sample caller serves its API: plain HTTP, as the gateway's route reaches it.
CALLER_PORT = 8080
# As every service's own pods run.
RUN_AS_SERVICE = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}


# ---------------------------------------------------------------------------------------- helpers


def kubectl(context: str, *arguments: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "--context", context, *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise AzureError(f"kubectl {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout


def apply(context: str, documents: Iterable[dict[str, Any]], *, server_side: bool = False) -> None:
    text = yaml.safe_dump_all(list(documents), sort_keys=False)
    extra = ["--server-side", "--force-conflicts"] if server_side else []
    kubectl(context, "apply", *extra, "--filename=-", stdin=text)


def rendered(directory: Path) -> list[dict[str, Any]]:
    """A Kustomize directory's documents."""
    text = subprocess.run(
        ["kubectl", "kustomize", str(directory)], capture_output=True, text=True, check=True
    ).stdout
    return [document for document in yaml.safe_load_all(text) if document]


def workload_service(workload: str) -> str:
    """windops-sample-caller is built from services/sample-caller-service."""
    return workload.removeprefix("windops-") + "-service"


def database_name(workload: str) -> str:
    """windops-sample-caller's database is windops_sample_caller, as locally."""
    return workload.replace("-", "_")


def principal_name(workload: str) -> str:
    """A workload's managed identity, and so its PostgreSQL login: id-windops-audit."""
    return f"id-{workload}"


def has_database(workload: str) -> bool:
    deployment = REPOSITORY / "services" / workload_service(workload) / "deploy" / "deployment.yaml"
    return f"name: {workload}-database" in deployment.read_text()


def image_tag() -> str:
    """The commit being deployed, so every image of a release has the same, unique tag."""
    source = (
        os.environ.get("BUILD_SOURCEVERSION")
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=REPOSITORY
        ).stdout
    )
    return source.strip()[:12]


def image_reference(registry: str, workload: str, tag: str) -> str:
    return f"{registry}.azurecr.io/{workload}:{tag}"


def client_ids(outputs: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item["clientId"] for item in outputs["workloadClientIds"]}


def principal_ids(outputs: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item["principalId"] for item in outputs["workloadClientIds"]}


def placeholder_values(outputs: dict[str, Any]) -> dict[str, str]:
    """The ${AZURE_...} values the generated overlays leave for the deployment."""
    from scripts.generate.azure_overlay import client_id_setting

    values = {
        "AZURE_KEYVAULT_NAME": outputs["keyVaultName"],
        "AZURE_TENANT_ID": outputs["tenantId"],
    }
    for workload, client_id in client_ids(outputs).items():
        values[client_id_setting(workload)] = client_id
    return values


def substitute(document: Any, values: dict[str, str]) -> Any:
    """Fill every ${AZURE_...} placeholder, refusing to apply one with no value."""
    text = json.dumps(document)

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise AzureError(f"No value for {name}")
        return values[name]

    return json.loads(PLACEHOLDER.sub(value, text))


# -------------------------------------------------------------------------------------- the steps


def push_images(registry: str, tag: str, workloads: Sequence[str] = WORKLOADS) -> None:
    """Build each service's image for the nodes' platform, and push it, signed in with Entra."""
    az("acr", "login", "--name", registry)
    for workload in workloads:
        reference = image_reference(registry, workload, tag)
        dockerfile = REPOSITORY / "services" / workload_service(workload) / "Dockerfile"
        print(f"Building {reference}")
        subprocess.run(
            ["docker", "build", "--quiet", "--platform", "linux/amd64",
             "--file", str(dockerfile), "--tag", reference, "."],
            cwd=REPOSITORY, check=True, stdout=subprocess.DEVNULL,
        )  # fmt: skip
        subprocess.run(["docker", "push", "--quiet", reference], check=True)


def spc_objects(document: dict[str, Any]) -> list[str]:
    """The Key Vault secrets a SecretProviderClass reads."""
    objects = yaml.safe_load(document["spec"]["parameters"]["objects"])["array"]
    return [yaml.safe_load(item)["objectName"] for item in objects]


def vault_values(outputs: dict[str, Any]) -> dict[str, str]:
    """Each service's database settings, as its SecretProviderClass names them. None is secret:
    the login is the service's own Entra identity, with no password."""
    values: dict[str, str] = {}
    for workload in DATABASE_WORKLOADS:
        prefix = f"{workload}-database--"
        values |= {
            f"{prefix}host": outputs["postgresHost"],
            f"{prefix}port": "5432",
            f"{prefix}database": database_name(workload),
            f"{prefix}username": principal_name(workload),
        }
    return values


def needed_secrets(documents: Iterable[dict[str, Any]]) -> set[str]:
    return {
        name
        for document in documents
        if document["kind"] == "SecretProviderClass"
        for name in spc_objects(document)
    }


def write_vault(vault: str, documents: Sequence[dict[str, Any]], outputs: dict[str, Any]) -> None:
    """Every secret the deployed services read, so none starts without one."""
    values = vault_values(outputs)
    needed = needed_secrets(documents)
    unknown = needed - set(values) - {SIGNING_KEY}
    if unknown:
        raise AzureError(f"Nothing provides these Key Vault secrets: {sorted(unknown)}")
    for name in sorted(needed & set(values)):
        az("keyvault", "secret", "set", "--vault-name", vault, "--name", name,
           "--value", values[name], "--output", "none")  # fmt: skip
    existing = az_json("keyvault", "secret", "list", "--vault-name", vault,
                       "--query", f"[?name=='{SIGNING_KEY}']")  # fmt: skip
    if not existing:
        print("Making the internal identity signing key, kept in Key Vault")
        # From a file, so the key never appears in a process listing.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.touch(mode=0o600)
            path.write_text(secrets.token_urlsafe(48))
            az("keyvault", "secret", "set", "--vault-name", vault, "--name", SIGNING_KEY,
               "--file", str(path), "--output", "none")  # fmt: skip


def settings_data(outputs: dict[str, Any], tag: str) -> dict[str, str]:
    """windops-settings on Azure: the shared defaults, with Azure's own values over them."""
    data = settings.cluster_settings()
    public = f"https://{outputs['gatewayFqdn']}"
    data |= {
        "WINDOPS_ENVIRONMENT": settings.load().get("AZURE_ENVIRONMENT") or "staging",
        "WINDOPS_VERSION": tag,
        # Entra sign-in and Service Bus follow from the environment (ADR 0006).
        "WINDOPS_DATABASE_AUTH": "auto",
        "WINDOPS_EVENTS_BACKEND": "auto",
        "WINDOPS_SERVICEBUS_NAMESPACE": outputs["serviceBusNamespace"],
        "WINDOPS_PUBLIC_URL": public,
        "WINDOPS_ALLOWED_ORIGINS": public,
    }
    return data


def settings_maps(outputs: dict[str, Any], tag: str) -> list[dict[str, Any]]:
    data = settings_data(outputs, tag)
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "windops-settings",
                "namespace": namespace,
                "labels": {"app.kubernetes.io/part-of": "windops"},
            },
            "data": data,
        }
        for namespace in SETTINGS_NAMESPACES
    ]


def ca_secret(pem: str) -> dict[str, Any]:
    """The internal CA, kept in Key Vault, as the Secret cert-manager's CA issuer reads."""
    blocks = re.findall(r"-----BEGIN [A-Z ]+-----.+?-----END [A-Z ]+-----\n?", pem, re.DOTALL)
    certificate = next(block for block in blocks if "CERTIFICATE" in block)
    key = next(block for block in blocks if "PRIVATE KEY" in block)
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "kubernetes.io/tls",
        "metadata": {"name": CA_SECRET, "namespace": "cert-manager"},
        "stringData": {"tls.crt": certificate, "tls.key": key, "ca.crt": certificate},
    }


def issuer_documents() -> list[dict[str, Any]]:
    """The local PKI without its self-signed CA: on Azure the CA comes from Key Vault, so App
    Gateway can trust it and it outlives the cluster."""
    documents = rendered(SECURITY / "pki")
    return [
        document
        for document in documents
        if not (document["kind"] == "ClusterIssuer" and "selfSigned" in document["spec"])
        and not (document["kind"] == "Certificate" and document["spec"].get("isCA"))
    ]


def install_cert_manager(context: str, vault: str) -> None:
    kubectl(context, "apply", "--server-side", "--force-conflicts",
            "--kustomize", str(SECURITY / "cert-manager"))  # fmt: skip
    for workload in ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector"):
        kubectl(context, "rollout", "status", f"deployment/{workload}",
                "--namespace=cert-manager", "--timeout=300s")  # fmt: skip
    pem = az("keyvault", "secret", "show", "--vault-name", vault, "--name", CA_SECRET,
             "--query", "value", "--output", "tsv")  # fmt: skip
    apply(context, [ca_secret(pem)])
    # cert-manager's webhook refuses requests for a while after it reports ready.
    for attempt in range(30):
        try:
            apply(context, issuer_documents())
            return
        except AzureError:
            if attempt == 29:
                raise
            time.sleep(5)


def monitoring_documents(outputs: dict[str, Any]) -> list[dict[str, Any]]:
    """The Collector for Application Insights, and managed Prometheus's scrape job."""
    documents = substitute(rendered(MONITORING), placeholder_values(outputs))
    for document in documents:
        document["metadata"].setdefault("namespace", OBSERVABILITY)
    return list(documents)


def postgres_documents(outputs: dict[str, Any], image: str) -> list[dict[str, Any]]:
    """The Job that gives each service its database and Entra login, as the server's admin."""
    principals = principal_ids(outputs)
    roles = [
        f"{principal_name(workload)}={principals[workload]}={database_name(workload)}"
        for workload in DATABASE_WORKLOADS
    ]
    command = [
        "python", "-m", "windops_migrations", "provision-entra",
        "--host", outputs["postgresHost"], "--admin", f"id-{POSTGRES_ADMIN}",
        *[item for role in roles for item in ("--role", role)],
    ]  # fmt: skip
    labels = {"app.kubernetes.io/name": "windops-postgres-provision",
              "app.kubernetes.io/part-of": "windops"}  # fmt: skip
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": POSTGRES_ADMIN,
            "namespace": DATA,
            "annotations": {"azure.workload.identity/client-id": outputs["postgresAdminClientId"]},
        },
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "windops-postgres-provision", "namespace": DATA, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 600,
            "template": {
                "metadata": {"labels": {**labels, "azure.workload.identity/use": "true"}},
                "spec": {
                    "serviceAccountName": POSTGRES_ADMIN,
                    "restartPolicy": "Never",
                    "securityContext": RUN_AS_SERVICE,
                    "containers": [
                        {
                            "name": "provision",
                            "image": image,
                            "command": command,
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "96Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],  # noqa: S108
                        }
                    ],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}],
                },
            },
        },
    }
    return [account, job]


def migration_job(path: Path, image: str, workload: str) -> dict[str, Any]:
    """A service's migration Job, signed in to PostgreSQL as the service, through Workload ID."""
    job: dict[str, Any] = yaml.safe_load(path.read_text())
    template = job["spec"]["template"]
    template["metadata"].setdefault("labels", {})["azure.workload.identity/use"] = "true"
    spec = template["spec"]
    spec["serviceAccountName"] = workload
    container = spec["containers"][0]
    container["image"] = image
    container["env"] = [
        item for item in container["env"] if item["name"] != "WINDOPS_DATABASE_PASSWORD"
    ] + [{"name": "WINDOPS_DATABASE_AUTH", "value": "entra"}]
    # Mounting the service's SecretProviderClass is what syncs its database Secret from Key Vault.
    container["volumeMounts"].append(
        {"name": "secrets-store", "mountPath": "/mnt/secrets-store", "readOnly": True}
    )
    spec["volumes"].append(
        {
            "name": "secrets-store",
            "csi": {
                "driver": CSI_DRIVER,
                "readOnly": True,
                "volumeAttributes": {"secretProviderClass": workload},
            },
        }
    )
    return job


def chosen(document: dict[str, Any], workloads: Sequence[str] = WORKLOADS) -> bool:
    metadata = document["metadata"]
    name = (metadata.get("labels") or {}).get("app.kubernetes.io/name") or metadata["name"]
    return document["kind"] == "Namespace" or name in workloads


def service_documents(
    outputs: dict[str, Any], registry: str, tag: str, workloads: Sequence[str] = WORKLOADS
) -> list[dict[str, Any]]:
    """The azure overlay's documents for the chosen services, filled in, with their images."""
    documents = [d for d in rendered(OVERLAY) if chosen(d, workloads)]
    documents = substitute(documents, placeholder_values(outputs))
    for document in documents:
        if document["kind"] == "Deployment":
            name = document["metadata"]["name"]
            for container in document["spec"]["template"]["spec"]["containers"]:
                container["image"] = image_reference(registry, name, tag)
    return list(documents)


DATABASE_WORKLOADS = tuple(workload for workload in WORKLOADS if has_database(workload))


# ---------------------------------------------------------------------------------------- command


def deploy_platform(config: dict[str, Any], outputs: dict[str, Any], context: str) -> int:
    registry, vault, tag = outputs["registryName"], outputs["keyVaultName"], image_tag()
    start_postgres(config)
    push_images(registry, tag)
    services = service_documents(outputs, registry, tag)
    print("Writing the services' settings to Key Vault")
    write_vault(vault, services, outputs)
    apply(context, [d for d in services if d["kind"] == "Namespace"])
    apply(context, settings_maps(outputs, tag))
    print("Installing cert-manager, with the internal CA from Key Vault")
    install_cert_manager(context, vault)
    print("Deploying the Collector for Application Insights")
    apply(context, monitoring_documents(outputs))
    # The service accounts and SecretProviderClasses first: the Jobs sign in with them.
    apply(context, [d for d in services if d["kind"] in ("ServiceAccount", "SecretProviderClass")])
    print("Giving each service its database and Entra login")
    provisioner = image_reference(registry, DATABASE_WORKLOADS[0], tag)
    account, job = postgres_documents(outputs, provisioner)
    apply(context, [account])
    try:
        run_migrations.run_job(yaml.safe_dump(job), context=context)
        for workload in DATABASE_WORKLOADS:
            path = REPOSITORY / "services" / workload_service(workload) / "deploy"
            manifest = migration_job(
                path / "migrate-job.yaml", image_reference(registry, workload, tag), workload
            )
            run_migrations.run_job(yaml.safe_dump(manifest), context=context)
    except run_migrations.MigrationError as error:
        raise AzureError(str(error)) from error
    print("Deploying the services")
    apply(context, services)
    for name, namespace in workload_namespaces(services).items():
        kubectl(context, "rollout", "status", f"deployment/{name}", f"--namespace={namespace}",
                "--timeout=300s")  # fmt: skip
    kubectl(context, "rollout", "status", "deployment/windops-otel-collector",
            f"--namespace={OBSERVABILITY}", "--timeout=300s")  # fmt: skip
    print(f"Deployed {len(WORKLOADS)} services, release {tag}")
    return 0


# ------------------------------------------------------------------------------------------ proof


def check(passed: bool, message: str) -> None:
    if not passed:
        raise AzureError(f"FAIL {message}")
    print(f"PASS {message}")


def workload_namespaces(documents: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {
        d["metadata"]["name"]: d["metadata"]["namespace"]
        for d in documents
        if d["kind"] == "Deployment"
    }


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def echo_through_mutual_tls(context: str, attempts: int = 30) -> dict[str, Any]:
    """The caller answers once it has called the responder over gRPC, with mutual TLS and a signed
    identity, which the responder requires. The caller serves its API on plain HTTP, port 8080, as
    the gateway's route reaches it, so this reaches it the same way, through a port-forward."""
    import socket

    import httpx

    port = free_port()
    process = subprocess.Popen(
        ["kubectl", "--context", context, "port-forward", "--namespace=windops-application",
         "deployment/windops-sample-caller", f"{port}:{CALLER_PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    last: Any = None
    try:
        # A port-forward takes a moment to listen: wait for it, or for it to stop.
        for _ in range(100):
            if process.poll() is not None:
                reason = process.stderr.read() if process.stderr else ""
                raise AzureError(f"The port-forward to the caller stopped: {reason}")
            with socket.socket() as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        with httpx.Client(timeout=8) as client:
            for _ in range(attempts):
                try:
                    response = client.get(
                        f"http://127.0.0.1:{port}/sample/echo", params={"message": "hello"}
                    )
                    if response.status_code == 200:
                        body: dict[str, Any] = response.json()
                        return body
                    last = f"{response.status_code} {response.text[:200]}"
                except httpx.HTTPError as error:
                    last = f"{type(error).__name__}: {error}"
                time.sleep(2)
    finally:
        process.terminate()
        process.wait(timeout=10)
    raise AzureError(f"The sample caller never answered: {last}")


def insights_query(app_id: str, query: str) -> int:
    """A count from Application Insights, signed in as the Azure CLI's identity."""
    found = az_json(
        "rest", "--method", "get",
        "--url", f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
        "--url-parameters", f"query={query}",
        "--resource", "https://api.applicationinsights.io",
    )  # fmt: skip
    return int(found["tables"][0]["rows"][0][0])


def traces_arrive(outputs: dict[str, Any], minutes: int = 10) -> int:
    """Wait for the echo's request in Application Insights: ingestion takes a few minutes."""
    app_id = az("resource", "show", "--resource-group", outputs["dataResourceGroup"],
                "--name", outputs["applicationInsightsName"],
                "--resource-type", "Microsoft.Insights/components",
                "--query", "properties.AppId", "--output", "tsv")  # fmt: skip
    query = "requests | where timestamp > ago(1h) | where name has '/sample/echo' | count"
    deadline = time.monotonic() + minutes * 60
    while True:
        count = insights_query(app_id, query)
        if count or time.monotonic() > deadline:
            return count
        time.sleep(20)


def prove_platform(outputs: dict[str, Any], context: str) -> None:
    """The services run on Azure with no stored password, and their telemetry reaches Azure."""
    registry, tag = outputs["registryName"], image_tag()
    namespaces = workload_namespaces(service_documents(outputs, registry, tag))
    for name, namespace in namespaces.items():
        kubectl(context, "wait", f"deployment/{name}", f"--namespace={namespace}",
                "--for=condition=Available", "--timeout=180s")  # fmt: skip
    check(True, f"{len(namespaces)} services are Available, with their Secrets from Key Vault")
    for workload in DATABASE_WORKLOADS:
        state = run_migrations.job_state(f"{workload}-migrate", namespaces[workload], context)
        check(state == "Complete", f"{workload}'s migrations ran, signed in to PostgreSQL by Entra")
    pods = json.loads(kubectl(context, "get", "pods", "--all-namespaces",
                              "--selector=azure.workload.identity/use=true",
                              "--output=json"))["items"]  # fmt: skip
    passwords = [
        f"{pod['metadata']['name']}: {item['name']}"
        for pod in pods
        for container in pod["spec"]["containers"]
        for item in container.get("env") or []
        if item["name"] in ("WINDOPS_DATABASE_PASSWORD", "WINDOPS_EVENTS_PASSWORD")
    ]
    check(not passwords, f"No Workload ID pod is given a password ({len(pods)} pods)")
    body = echo_through_mutual_tls(context)
    check(
        body == {"message": "hello", "tenant_id": "sample"},
        "The caller reached the responder over mutual TLS with a signed identity",
    )
    count = traces_arrive(outputs)
    check(count > 0, f"Application Insights has the echo's trace ({count} requests)")
