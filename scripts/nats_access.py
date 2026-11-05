"""NATS logins for each service, shared by `make secrets`, `make infra` and `make nats-check`.

The configuration Secret stays in windops-data, beside the administrator password. Each login can
publish only the subjects its service owns, so no service can forge another's events.
"""

import json
from pathlib import Path

from provision_postgres import ADMIN_KEY, SERVICE_NAMESPACES, database_name
from windops_events import audit_request_subject

NAMESPACE = "windops-data"
ADMIN_USER = "windops_admin"
URL = f"nats://windops-nats.{NAMESPACE}.svc.cluster.local:4222"
SECRETS_FILE = (
    Path(__file__).resolve().parent.parent
    / "infrastructure"
    / "minikube"
    / "secrets.local.nats.env"
)
# Web BFF stores nothing, but listens for live progress (API-003).
EVENT_SERVICES = {**SERVICE_NAMESPACES, "web-bff": "windops-system"}
# Who owns each subject. docs/service-map.md shows the same, and a test keeps them matched.
PUBLISHERS = {
    "telemetry": ("telemetry.received", "telemetry.quality"),
    "alarm": ("alarm.changed",),
    "fault-detection": ("fault.detected",),
    "investigation": ("case.updated",),
    "asset": ("evidence.changed",),
    "knowledge": ("evidence.changed",),
    "inventory": ("evidence.changed",),
    "maintenance": ("workorder.submitted", "workorder.completed"),
    "approval": ("approval.decided",),
    "audit": ("audit.recorded",),
}
# Only the administrator may change streams.
_DENIED_API = [
    "$JS.API.STREAM.CREATE.>",
    "$JS.API.STREAM.UPDATE.>",
    "$JS.API.STREAM.DELETE.>",
    "$JS.API.STREAM.PURGE.>",
    "$JS.API.STREAM.RESTORE.>",
    "$JS.API.STREAM.MSG.DELETE.>",
]


def _service_user(service: str, password: str) -> dict[str, object]:
    return {
        "user": database_name(service),
        "password": password,
        "permissions": {
            "publish": {
                "allow": [
                    *PUBLISHERS.get(service, ()),
                    # Its own subject only, and only with an outbox to send it from (EVT-004).
                    *([audit_request_subject(service)] if service in SERVICE_NAMESPACES else []),
                    "deadletter.>",
                    "$JS.API.>",
                    "$JS.ACK.>",
                    "_INBOX.>",
                ],
                "deny": _DENIED_API,
            },
            "subscribe": {"allow": ["_INBOX.>"]},
        },
    }


def configuration(passwords: dict[str, str]) -> str:
    """Return the server's configuration. NATS reads JSON, which also makes every value safe."""
    users: list[dict[str, object]] = [{"user": ADMIN_USER, "password": passwords[ADMIN_KEY]}]
    users += [_service_user(service, passwords[service]) for service in EVENT_SERVICES]
    return json.dumps(
        {
            "server_name": "windops-nats-0",
            "port": 4222,
            "http_port": 8222,
            "jetstream": {
                "store_dir": "/data",
                "max_memory_store": 16 * 1024 * 1024,
                "max_file_store": 2 * 1024 * 1024 * 1024,
                # Limits what a sudden machine failure can lose. A pod restart loses nothing.
                "sync_interval": "10s",
            },
            "authorization": {"users": users},
        },
        indent=2,
    )
