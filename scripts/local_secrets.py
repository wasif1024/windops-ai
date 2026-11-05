#!/usr/bin/env python3
"""Create the local cluster's secrets from git-ignored files.

Each file is generated once, with a random value, and then reused so tokens stay valid. Each
secret goes only to the namespaces that need it.
"""

import json
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from nats_access import ADMIN_USER as NATS_ADMIN_USER
from nats_access import EVENT_SERVICES, configuration
from nats_access import SECRETS_FILE as NATS_SECRETS_FILE
from nats_access import URL as NATS_URL
from provision_postgres import (
    ADMIN_KEY,
    ADMIN_USER,
    HOST,
    PLATFORM_DATABASES,
    PORT,
    SECRETS_FILE,
    SERVICE_NAMESPACES,
    database_name,
    read_passwords,
)
from redis_access import ADMIN_USER as REDIS_ADMIN_USER
from redis_access import HOST as REDIS_HOST
from redis_access import PORT as REDIS_PORT
from redis_access import SECRETS_FILE as REDIS_SECRETS_FILE
from redis_access import acl_file

CONTEXT = "windops"
REPOSITORY = Path(__file__).resolve().parent.parent
LOCAL_DIRECTORY = REPOSITORY / "infrastructure" / "minikube"
NAMESPACES_MANIFEST = REPOSITORY / "infrastructure" / "kubernetes" / "base" / "namespaces"


@dataclass(frozen=True)
class LocalSecret:
    name: str
    key: str
    file_name: str
    namespaces: tuple[str, ...]


LOCAL_SECRETS = (
    LocalSecret(
        name="windops-internal-identity",
        key="signing-key",
        file_name="secrets.local.env",
        namespaces=("windops-system", "windops-application", "windops-mcp"),
    ),
    LocalSecret(
        name="windops-gateway-admin",
        key="admin-key",
        file_name="secrets.local.gateway.env",
        namespaces=("windops-system",),
    ),
    LocalSecret(
        name="windops-qdrant-api-key",
        key="api-key",
        file_name="secrets.local.qdrant.env",
        # Knowledge is its only client, and runs in the application namespace (SRS 11.2).
        namespaces=("windops-data", "windops-application"),
    ),
    LocalSecret(
        name="windops-keycloak-admin",
        key="password",
        file_name="secrets.local.keycloak.env",
        namespaces=("windops-system",),
    ),
    LocalSecret(
        name="windops-grafana-admin",
        key="admin-password",
        file_name="secrets.local.grafana.env",
        namespaces=("windops-observability",),
    ),
)


def ensure_secrets_file(path: Path, key: str) -> bool:
    """Create the file if needed, readable only by its owner. Return whether it was created."""
    if path.exists():
        return False
    path.touch(mode=0o600)
    path.write_text(f"{key}={secrets.token_urlsafe(48)}\n")
    return True


def apply_secret(name: str, namespace: str, path: Path) -> None:
    rendered = subprocess.run(
        [
            "kubectl", "create", "secret", "generic", name,
            f"--namespace={namespace}", f"--from-env-file={path}",
            "--dry-run=client", "--output=yaml",
        ],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    subprocess.run(
        ["kubectl", "--context", CONTEXT, "apply", "--filename=-"],
        input=rendered, capture_output=True, text=True, check=True,
    )  # fmt: skip


def ensure_passwords(path: Path, keys: list[str]) -> list[str]:
    """Add a password for each missing key, keeping existing ones. Return the keys added."""
    path.touch(mode=0o600)
    existing = read_passwords(path)
    added = [key for key in keys if key not in existing]
    with path.open("a") as file:
        for key in added:
            file.write(f"{key}={secrets.token_urlsafe(32)}\n")
    return added


def postgres_secrets(passwords: dict[str, str]) -> list[dict[str, object]]:
    """Return the admin Secret and one Secret per service, each only in its own namespace."""
    manifests = [
        secret_manifest(
            "windops-postgres-admin",
            "windops-data",
            {"username": ADMIN_USER, "password": passwords[ADMIN_KEY]},
        )
    ]
    for service, namespace in {**SERVICE_NAMESPACES, **PLATFORM_DATABASES}.items():
        name = database_name(service)
        manifests.append(
            secret_manifest(
                f"windops-{service}-database",
                namespace,
                {
                    "host": HOST,
                    "port": PORT,
                    "database": name,
                    "username": name,
                    "password": passwords[service],
                },
            )
        )
    return manifests


def secret_manifest(name: str, namespace: str, values: dict[str, str]) -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "stringData": values,
    }


def apply_postgres_secrets() -> None:
    added = ensure_passwords(SECRETS_FILE, [ADMIN_KEY, *SERVICE_NAMESPACES, *PLATFORM_DATABASES])
    print(f"Added {len(added)} passwords to {SECRETS_FILE.name}")
    apply_manifests(postgres_secrets(read_passwords(SECRETS_FILE)))
    print(f"Applied PostgreSQL credentials for {len(SERVICE_NAMESPACES)} services")


def redis_secrets(passwords: dict[str, str]) -> list[dict[str, object]]:
    """Return the ACL and admin Secrets, and one Secret per service in its own namespace."""
    manifests = [
        secret_manifest("windops-redis-acl", "windops-data", {"users.acl": acl_file(passwords)}),
        secret_manifest(
            "windops-redis-admin",
            "windops-data",
            {"username": REDIS_ADMIN_USER, "password": passwords[ADMIN_KEY]},
        ),
    ]
    for service, namespace in SERVICE_NAMESPACES.items():
        manifests.append(
            secret_manifest(
                f"windops-{service}-cache",
                namespace,
                {
                    "host": REDIS_HOST,
                    "port": REDIS_PORT,
                    "username": database_name(service),
                    "password": passwords[service],
                },
            )
        )
    return manifests


def apply_manifests(manifests: list[dict[str, object]]) -> None:
    # Sent over stdin, so no password appears in a process listing.
    for manifest in manifests:
        subprocess.run(
            ["kubectl", "--context", CONTEXT, "apply", "--filename=-"],
            input=json.dumps(manifest), capture_output=True, text=True, check=True,
        )  # fmt: skip


def nats_secrets(passwords: dict[str, str]) -> list[dict[str, object]]:
    """Return the server's configuration, and one Secret per service in its own namespace."""
    manifests = [
        secret_manifest(
            "windops-nats-config", "windops-data", {"nats.conf": configuration(passwords)}
        ),
        secret_manifest(
            "windops-nats-admin",
            "windops-data",
            {"username": NATS_ADMIN_USER, "password": passwords[ADMIN_KEY]},
        ),
    ]
    for service, namespace in EVENT_SERVICES.items():
        manifests.append(
            secret_manifest(
                f"windops-{service}-events",
                namespace,
                {
                    "url": NATS_URL,
                    "username": database_name(service),
                    "password": passwords[service],
                },
            )
        )
    return manifests


def apply_nats_secrets() -> None:
    added = ensure_passwords(NATS_SECRETS_FILE, [ADMIN_KEY, *EVENT_SERVICES])
    print(f"Added {len(added)} passwords to {NATS_SECRETS_FILE.name}")
    apply_manifests(nats_secrets(read_passwords(NATS_SECRETS_FILE)))
    print(f"Applied NATS credentials for {len(EVENT_SERVICES)} services")


def apply_redis_secrets() -> None:
    added = ensure_passwords(REDIS_SECRETS_FILE, [ADMIN_KEY, *SERVICE_NAMESPACES])
    print(f"Added {len(added)} passwords to {REDIS_SECRETS_FILE.name}")
    apply_manifests(redis_secrets(read_passwords(REDIS_SECRETS_FILE)))
    print(f"Applied Redis credentials for {len(SERVICE_NAMESPACES)} services")


def main() -> int:
    try:
        subprocess.run(
            ["kubectl", "--context", CONTEXT, "apply", f"--kustomize={NAMESPACES_MANIFEST}"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        for item in LOCAL_SECRETS:
            path = LOCAL_DIRECTORY / item.file_name
            created = ensure_secrets_file(path, item.key)
            print(f"{'Created' if created else 'Using'} {item.file_name}")
            for namespace in item.namespaces:
                apply_secret(item.name, namespace, path)
            print(f"Applied the {item.key} to {', '.join(item.namespaces)}")
        apply_postgres_secrets()
        apply_redis_secrets()
        apply_nats_secrets()
    except subprocess.CalledProcessError as error:
        # Never print the rendered manifest, because it contains the key.
        print(f"Could not apply a secret: {error.stderr}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
