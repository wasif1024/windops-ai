#!/usr/bin/env python3
"""Create the local cluster's secrets from git-ignored files.

Each file is generated once, with a random value, and then reused so tokens stay valid. Each
secret goes only to the namespaces that need it.
"""

import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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
    except subprocess.CalledProcessError as error:
        # Never print the rendered manifest, because it contains the key.
        print(f"Could not apply a secret: {error.stderr}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
