"""Redis logins for each service, shared by `make secrets` and `make redis-check`.

The ACL file holds only password hashes. Each login reaches only its own key prefix (ARC-002), and
only the commands windops-cache uses.
"""

import hashlib
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


def _hashed(password: str) -> str:
    return "#" + hashlib.sha256(password.encode()).hexdigest()


def acl_file(passwords: dict[str, str]) -> str:
    """Return the ACL file. The default login is off, so every connection needs a password."""
    lines = [
        "user default off resetchannels -@all",
        f"user {ADMIN_USER} on {_hashed(passwords[ADMIN_KEY])} ~* &* +@all",
    ]
    for service in SERVICE_NAMESPACES:
        lines.append(
            f"user {database_name(service)} on {_hashed(passwords[service])} "
            f"~{service}:* resetchannels {SERVICE_COMMANDS}"
        )
    return "\n".join(lines) + "\n"
