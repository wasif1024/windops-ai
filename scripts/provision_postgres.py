#!/usr/bin/env python3
"""Create one database and one login per service, and set each password from the local file.

Safe to run repeatedly. It adds new services and restores settings, and it never removes data.
"""

import re
import subprocess
import sys
from pathlib import Path

CONTEXT = "windops"
NAMESPACE = "windops-data"
POD = "windops-postgres-0"
ADMIN_USER = "windops_admin"
HOST = f"windops-postgres.{NAMESPACE}.svc.cluster.local"
PORT = "5432"
SECRETS_FILE = (
    Path(__file__).resolve().parent.parent
    / "infrastructure"
    / "minikube"
    / "secrets.local.postgres.env"
)
ADMIN_KEY = "admin"
# Each SRS 7.2 service that stores state, and its namespace from SRS 11.2. The gateway uses etcd,
# and Web BFF and the sandbox adapter keep no state of their own (ARC-005).
SERVICE_NAMESPACES = {
    "identity": "windops-system",
    "authorization": "windops-system",
    "tenant": "windops-system",
    "asset": "windops-application",
    "telemetry": "windops-application",
    "alarm": "windops-application",
    "fault-detection": "windops-application",
    "investigation": "windops-application",
    "agent-runtime": "windops-application",
    "knowledge": "windops-application",
    "weather": "windops-application",
    "external-intelligence": "windops-application",
    "production-impact": "windops-application",
    "policy": "windops-application",
    "maintenance": "windops-application",
    "inventory": "windops-application",
    "approval": "windops-application",
    "audit": "windops-application",
    "notification": "windops-application",
    "evaluation": "windops-application",
}
_SAFE_PASSWORD = re.compile(r"[A-Za-z0-9_-]+")
_SAFE_SERVICE = re.compile(r"[a-z]+(-[a-z]+)*")


def database_name(service: str) -> str:
    """Return the name shared by a service's database and login."""
    return f"windops_{service.replace('-', '_')}"


def read_passwords(path: Path = SECRETS_FILE) -> dict[str, str]:
    """Read the password file, whose keys are `admin` and the service names."""
    entries = (line.partition("=") for line in path.read_text().splitlines() if line)
    return {key: value for key, _, value in entries}


def provisioning_sql(passwords: dict[str, str]) -> str:
    """Return the SQL that isolates every service in `passwords`."""
    # Failed statements would otherwise be logged, including the password they contain.
    lines = [
        "SET log_statement = 'none';",
        "SET log_min_error_statement = 'panic';",
        "REVOKE ALL ON DATABASE postgres FROM PUBLIC;",
        "REVOKE ALL ON DATABASE template1 FROM PUBLIC;",
        # The image creates a database named after its administrator, open to every login.
        f"REVOKE ALL ON DATABASE {ADMIN_USER} FROM PUBLIC;",
    ]
    for service, password in passwords.items():
        # SQL cannot take a role or database name as a parameter, so both inputs are validated.
        if not _SAFE_SERVICE.fullmatch(service):
            raise ValueError(f"Unsupported service name: {service}")
        if not _SAFE_PASSWORD.fullmatch(password):
            raise ValueError(f"The password for {service} has unsupported characters")
        name = database_name(service)
        lines += [
            f"SELECT 'CREATE ROLE {name}' WHERE NOT EXISTS "  # noqa: S608
            f"(SELECT FROM pg_roles WHERE rolname = '{name}') \\gexec",
            # A service must not gain any power beyond its own database (ARC-002, DAT-005).
            f"ALTER ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            f"NOBYPASSRLS PASSWORD '{password}';",
            f"SELECT 'CREATE DATABASE {name} OWNER {name}' WHERE NOT EXISTS "  # noqa: S608
            f"(SELECT FROM pg_database WHERE datname = '{name}') \\gexec",
            f"ALTER DATABASE {name} OWNER TO {name};",
            f"REVOKE ALL ON DATABASE {name} FROM PUBLIC;",
        ]
    return "\n".join(lines) + "\n"


def redact(text: str, secrets: list[str]) -> str:
    """Remove secrets from text, because psql errors quote the failing statement."""
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def main() -> int:
    if not SECRETS_FILE.exists():
        print("No PostgreSQL passwords found. Run: make secrets", file=sys.stderr)
        return 1
    passwords = read_passwords()
    missing = sorted(set(SERVICE_NAMESPACES) - set(passwords))
    if missing:
        print(f"Missing passwords for {', '.join(missing)}. Run: make secrets", file=sys.stderr)
        return 1
    passwords.pop(ADMIN_KEY, None)
    services = {service: passwords[service] for service in SERVICE_NAMESPACES}

    subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "rollout", "status", "statefulset/windops-postgres",
            f"--namespace={NAMESPACE}", "--timeout=180s",
        ],
        check=True,
    )  # fmt: skip
    # The local socket avoids sending the admin password. Stdin keeps service passwords out of
    # process listings.
    result = subprocess.run(
        [
            "kubectl", "--context", CONTEXT, "exec", "--stdin", POD, f"--namespace={NAMESPACE}",
            "--", "psql", "--no-psqlrc", "--quiet", "--set=ON_ERROR_STOP=1",
            f"--username={ADMIN_USER}", "--dbname=postgres",
        ],
        input=provisioning_sql(services), capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        print(
            f"Could not provision PostgreSQL: {redact(result.stderr, list(services.values()))}",
            file=sys.stderr,
        )
        return 1
    print(f"Provisioned {len(services)} service databases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
