#!/usr/bin/env python3
"""Prove PostgreSQL works on the real local cluster.

Run `make secrets` and `make infra` first. A client pod connects through the Service, as a real
service would. The check restarts PostgreSQL once, and removes its pod and test table afterwards.
"""

import subprocess
import sys
import time

from provision_postgres import (
    ADMIN_USER,
    CONTEXT,
    HOST,
    NAMESPACE,
    POD,
    SECRETS_FILE,
    SERVICE_NAMESPACES,
    database_name,
    read_passwords,
)

CLIENT = "windops-postgres-check-client"
CLIENT_NAMESPACE = "windops-application"
STATEFULSET = "statefulset/windops-postgres"
TELEMETRY = "telemetry"
TELEMETRY_DATABASE = database_name(TELEMETRY)
CLIENT_POD = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {CLIENT}
  namespace: {CLIENT_NAMESPACE}
spec:
  automountServiceAccountToken: false
  terminationGracePeriodSeconds: 0
  securityContext:
    runAsNonRoot: true
    runAsUser: 70
    runAsGroup: 70
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: client
      image: IMAGE
      command: [sleep, "600"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
"""


def kubectl(*arguments: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip


def required(*arguments: str, stdin: str | None = None) -> str:
    result = kubectl(*arguments, stdin=stdin)
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def query(
    user: str, database: str, password: str, sql: str, *, host: str = HOST
) -> subprocess.CompletedProcess[str]:
    """Run SQL from the client pod over the network, with the password sent on stdin."""
    script = 'PGPASSWORD="$(cat)" exec psql "$@"'
    return kubectl(
        "exec", "--stdin", CLIENT, f"--namespace={CLIENT_NAMESPACE}", "--",
        "sh", "-c", script, "psql", "--no-psqlrc", "--tuples-only", "--no-align",
        "--set=ON_ERROR_STOP=1", f"--host={host}", f"--username={user}", f"--dbname={database}",
        f"--command={sql}",
        stdin=password,
    )  # fmt: skip


def admin_sql(database: str, sql: str) -> str:
    """Run SQL as the administrator, through the pod's local socket."""
    return required(
        "exec", POD, f"--namespace={NAMESPACE}", "--", "psql", "--no-psqlrc", "--tuples-only",
        "--no-align", "--set=ON_ERROR_STOP=1", f"--username={ADMIN_USER}", f"--dbname={database}",
        f"--command={sql}",
    ).strip()  # fmt: skip


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def start_client() -> None:
    image = required(
        "get", STATEFULSET, f"--namespace={NAMESPACE}",
        "--output=jsonpath={.spec.template.spec.containers[0].image}",
    )  # fmt: skip
    required("apply", "--filename=-", stdin=CLIENT_POD.replace("IMAGE", image))
    required(
        "wait", f"pod/{CLIENT}", f"--namespace={CLIENT_NAMESPACE}", "--for=condition=Ready",
        "--timeout=120s",
    )  # fmt: skip


def stop_client() -> None:
    kubectl(
        "delete", "pod", CLIENT, f"--namespace={CLIENT_NAMESPACE}", "--ignore-not-found", "--now"
    )


def check_isolation(passwords: dict[str, str]) -> None:
    services = list(SERVICE_NAMESPACES)
    for index, service in enumerate(services):
        name = database_name(service)
        other = database_name(services[(index + 1) % len(services)])
        own = query(name, name, passwords[service], "SELECT current_user")
        check(own.stdout.strip() == name, f"{service} can reach its own database")
        denied = query(name, other, passwords[service], "SELECT 1")
        check(
            denied.returncode != 0 and "permission denied" in denied.stderr,
            f"{service} is denied another service's database",
        )
    name = database_name(services[0])
    check(
        "permission denied" in query(name, "postgres", passwords[services[0]], "SELECT 1").stderr,
        "a service is denied the administrative database",
    )
    check(
        "authentication failed" in query(name, name, "wrong-password", "SELECT 1").stderr,
        "a wrong password is refused",
    )
    powers = admin_sql(
        "postgres",
        "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'windops\\_%' "
        "AND rolname <> 'windops_admin' AND (rolsuper OR rolcreatedb OR rolcreaterole "
        "OR rolreplication OR rolbypassrls)",
    )
    check(powers == "0", "no service login has administrator powers")


def check_passwords_are_scram() -> None:
    weak = admin_sql(
        "postgres",
        "SELECT count(*) FROM pg_authid "
        "WHERE rolcanlogin AND rolpassword NOT LIKE 'SCRAM-SHA-256%'",
    )
    check(weak == "0", "every password is stored as a SCRAM hash")


def check_no_extensions() -> None:
    for service in SERVICE_NAMESPACES:
        extensions = admin_sql(
            database_name(service), "SELECT string_agg(extname, ',') FROM pg_extension"
        )
        if extensions != "plpgsql":
            raise RuntimeError(f"{service} has unexpected extensions: {extensions}")
    check(True, "service databases use no extensions (ADR 0003)")


def check_partitions(password: str) -> None:
    """SRS 8.3: a service's login can create time partitions, and retention can drop one."""
    sql = """
    CREATE TABLE postgres_check_samples (event_time timestamptz NOT NULL, value integer NOT NULL)
        PARTITION BY RANGE (event_time);
    CREATE TABLE postgres_check_samples_old PARTITION OF postgres_check_samples
        FOR VALUES FROM ('2026-01-01') TO ('2026-01-02');
    CREATE TABLE postgres_check_samples_new PARTITION OF postgres_check_samples
        FOR VALUES FROM ('2026-01-02') TO ('2026-01-03');
    INSERT INTO postgres_check_samples
        VALUES ('2026-01-01 12:00+00', 1), ('2026-01-02 12:00+00', 2);
    DROP TABLE postgres_check_samples_old;
    SELECT count(*) FROM postgres_check_samples;
    """
    result = query(TELEMETRY_DATABASE, TELEMETRY_DATABASE, password, sql)
    check(result.stdout.split()[-1:] == ["1"], "dropping a time partition removes only its rows")


def count_rows(password: str, *, attempts: int = 1) -> str:
    sql = "SELECT count(*) FROM postgres_check_samples"
    for attempt in range(attempts):
        result = query(TELEMETRY_DATABASE, TELEMETRY_DATABASE, password, sql)
        if result.returncode == 0:
            return result.stdout.strip()
        if attempt + 1 < attempts:
            time.sleep(2)
    raise RuntimeError(f"Could not count rows: {result.stderr}")


def check_restart(password: str) -> None:
    required("delete", "pod", POD, f"--namespace={NAMESPACE}")
    required("rollout", "status", STATEFULSET, f"--namespace={NAMESPACE}", "--timeout=180s")
    check(count_rows(password, attempts=30) == "1", "data survives a pod restart (DEP-006)")
    logs = required("logs", POD, f"--namespace={NAMESPACE}", "--tail=100")
    check("was not properly shut down" not in logs, "PostgreSQL shuts down cleanly (DEP-004)")


def check_backup_and_restore(password: str) -> None:
    """Run the commands that infrastructure/data/postgres/README.md documents."""
    target = ["kubectl", "--context", CONTEXT, f"--namespace={NAMESPACE}", "exec"]
    dump = subprocess.run(
        [*target, POD, "--", "pg_dump", f"--username={ADMIN_USER}", "--format=custom",
         TELEMETRY_DATABASE],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    query(TELEMETRY_DATABASE, TELEMETRY_DATABASE, password, "DELETE FROM postgres_check_samples")
    check(count_rows(password) == "0", "test rows are gone before the restore")
    subprocess.run(
        [*target, "--stdin", POD, "--", "pg_restore", f"--username={ADMIN_USER}",
         f"--dbname={TELEMETRY_DATABASE}", "--clean", "--if-exists"],
        input=dump, capture_output=True, check=True,
    )  # fmt: skip
    check(count_rows(password) == "1", "a backup restores the data (DEP-006)")
    owner = admin_sql(
        TELEMETRY_DATABASE,
        "SELECT tableowner FROM pg_tables WHERE tablename = 'postgres_check_samples'",
    )
    check(owner == TELEMETRY_DATABASE, "a restored table keeps its service as owner")


def main() -> int:
    if not SECRETS_FILE.exists():
        print("No PostgreSQL passwords found. Run: make secrets", file=sys.stderr)
        return 1
    passwords = read_passwords()
    telemetry_password = passwords[TELEMETRY]
    try:
        start_client()
        check_isolation(passwords)
        check_passwords_are_scram()
        check_no_extensions()
        check_partitions(telemetry_password)
        check_restart(telemetry_password)
        check_backup_and_restore(telemetry_password)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        # Not `admin_sql`, because a cleanup failure must not hide the original one.
        kubectl(
            "exec", POD, f"--namespace={NAMESPACE}", "--", "psql", "--no-psqlrc",
            f"--username={ADMIN_USER}", f"--dbname={TELEMETRY_DATABASE}",
            "--command=DROP TABLE IF EXISTS postgres_check_samples",
        )  # fmt: skip
        stop_client()
    print("PostgreSQL check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
