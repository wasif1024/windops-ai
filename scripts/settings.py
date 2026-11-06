#!/usr/bin/env python3
"""WindOps' settings, so a value that differs between environments changes in one place.

Each setting is read from, in order: the process environment, which is how CI and a cloud
pipeline set them; then `.env`, your own copy, which git ignores; then the default in
`.env.example`. `make config` puts the cluster's settings into the `windops-settings`
ConfigMap, where every service, Keycloak and the gateway read them.

    python3 scripts/settings.py WINDOPS_PUBLIC_URL   # print one setting
    python3 scripts/settings.py --ensure-env         # create .env, or add the settings it lacks

Only the standard library, so the Makefile can run it before anything is installed.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
LOCAL = ROOT / ".env"
PREFIX = "WINDOPS_"
# For this machine's tools, not the cluster, so they stay out of the ConfigMap.
MACHINE_ONLY = frozenset({"WINDOPS_KUBE_CONTEXT", "WINDOPS_DEV_PORT"})
# A ConfigMap is not secret, so a name like these never goes into one.
SECRET_ENDINGS = ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN")


def parse(path: Path) -> dict[str, str]:
    """Read NAME=value lines. Blank lines and lines starting with # are skipped."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def load() -> dict[str, str]:
    merged = parse(EXAMPLE)
    merged.update({name: value for name, value in parse(LOCAL).items() if value != ""})
    for name in list(merged):
        if os.environ.get(name):
            merged[name] = os.environ[name]
    return merged


def get(name: str) -> str:
    value = load().get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set. Add it to .env (see .env.example)")
    return value


def cluster_settings() -> dict[str, str]:
    """The settings every workload in the cluster reads, from the ConfigMap."""
    chosen = {
        name: value
        for name, value in load().items()
        if name.startswith(PREFIX) and name not in MACHINE_ONLY
    }
    secret = [name for name in chosen if name.endswith(SECRET_ENDINGS)]
    if secret:
        raise RuntimeError(f"These look secret, so they cannot go in a ConfigMap: {secret}")
    return chosen


def ensure_env_file() -> list[str]:
    """Create .env from the example, or add the settings it lacks. Return the names added."""
    example = parse(EXAMPLE)
    if not LOCAL.exists():
        LOCAL.write_text(EXAMPLE.read_text())
        LOCAL.chmod(0o600)
        return list(example)
    present = parse(LOCAL)
    missing = [name for name in example if name not in present]
    if missing:
        with LOCAL.open("a") as file:
            file.write("\n# Added from .env.example\n")
            for name in missing:
                file.write(f"{name}={example[name]}\n")
    return missing


def origins() -> list[str]:
    """The extra browser origins allowed to call the API and sign in, such as the dev server."""
    return [
        item.strip().rstrip("/")
        for item in get("WINDOPS_ALLOWED_ORIGINS").split(",")
        if item.strip()
    ]


KUBE_CONTEXT = get("WINDOPS_KUBE_CONTEXT")
# The gateway's public address, which the browser and every token's issuer use.
PUBLIC_URL = get("WINDOPS_PUBLIC_URL").rstrip("/")
PUBLIC_PORT = urlparse(PUBLIC_URL).port or 443
REALM = get("WINDOPS_KEYCLOAK_REALM")
WEB_CLIENT_ID = get("WINDOPS_WEB_CLIENT_ID")
API_AUDIENCE = get("WINDOPS_OIDC_AUDIENCE")
KEYCLOAK_INTERNAL_URL = get("WINDOPS_KEYCLOAK_INTERNAL_URL").rstrip("/")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    if sys.argv[1] == "--ensure-env":
        added = ensure_env_file()
        print(f"Added to .env: {', '.join(added)}" if added else ".env has every setting.")
        return 0
    try:
        print(get(sys.argv[1]))
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
