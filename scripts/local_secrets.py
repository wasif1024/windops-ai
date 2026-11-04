#!/usr/bin/env python3
"""Create the local cluster's secrets from a git-ignored file.

The file is generated once, with a random key, and then reused so tokens stay valid.
"""

import secrets
import subprocess
import sys
from pathlib import Path

CONTEXT = "windops"
SECRET_NAME = "windops-internal-identity"  # noqa: S105  # A name, not a value.
NAMESPACES = ("windops-system", "windops-application", "windops-mcp")
SECRETS_FILE = (
    Path(__file__).resolve().parent.parent / "infrastructure" / "minikube" / "secrets.local.env"
)


def ensure_secrets_file(path: Path) -> bool:
    """Create the file if needed, readable only by its owner. Return whether it was created."""
    if path.exists():
        return False
    path.touch(mode=0o600)
    path.write_text(f"signing-key={secrets.token_urlsafe(48)}\n")
    return True


def apply_secret(namespace: str, path: Path) -> None:
    rendered = subprocess.run(
        [
            "kubectl", "create", "secret", "generic", SECRET_NAME,
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
    created = ensure_secrets_file(SECRETS_FILE)
    print(f"{'Created' if created else 'Using'} {SECRETS_FILE.name}")
    try:
        for namespace in NAMESPACES:
            apply_secret(namespace, SECRETS_FILE)
    except subprocess.CalledProcessError as error:
        # Never print the rendered manifest, because it contains the key.
        print(f"Could not apply the secret: {error.stderr}", file=sys.stderr)
        return 1
    print(f"Applied {SECRET_NAME} to {', '.join(NAMESPACES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
