"""Redis logins for each service, shared by `make secrets` and `make redis-check`.

Each login reaches only its own key prefix (ARC-002), and only the commands windops-cache uses. The
ACL Secret stays in windops-data, beside the administrator password, so it reveals nothing that a
reader of that namespace could not already reach. Redis hashes these passwords when it loads them.
"""

import re
from pathlib import Path

from provision_postgres import ADMIN_KEY, SERVICE_NAMESPACES, database_name

NAMESPACE = "windops-data"
ADMIN_USER = "windops_admin"
HOST = f"windops-redis.{NAMESPACE}.svc.cluster.local"
PORT = "6379"
SECRETS_FILE = (
    Path(__file__).resolve().parent.parent
    / "infrastructure"
    / "minikube"
    / "secrets.local.redis.env"
)
SERVICE_COMMANDS = "-@all +get +set +del +incrby +expire +multi +exec +ping +client|setinfo"


_SAFE_PASSWORD = re.compile(r"[A-Za-z0-9_-]+")


def _password_rule(password: str) -> str:
    # A space or newline would start another ACL rule, or another login.
    if not _SAFE_PASSWORD.fullmatch(password):
        raise ValueError("A Redis password has unsupported characters")
    return f">{password}"


# Web BFF stores nothing, but counts each user's requests here (SRS 7.5).
CACHE_SERVICES = {**SERVICE_NAMESPACES, "web-bff": "windops-system"}


def acl_file(passwords: dict[str, str]) -> str:
    """Return the ACL file. The default login is off, so every connection needs a password."""
    lines = [
        "user default off resetchannels -@all",
        f"user {ADMIN_USER} on {_password_rule(passwords[ADMIN_KEY])} ~* &* +@all",
    ]
    for service in CACHE_SERVICES:
        lines.append(
            f"user {database_name(service)} on {_password_rule(passwords[service])} "
            f"~{service}:* resetchannels {SERVICE_COMMANDS}"
        )
    return "\n".join(lines) + "\n"
