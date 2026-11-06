#!/usr/bin/env python3
"""Prove the event metrics, dashboards and alerts work on the cluster, using real failures.

Run `make secrets` and `make infra` first. This redeploys the Audit Service, and stops it and NATS
for a moment. Its test events are removed afterwards. `WindopsDeadLetterMessages` stays active for
up to an hour, because it looks back that far.
"""

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from nats.errors import Error as NatsError
from windops.events.v1 import events_pb2
from windops_events import audit_request_subject, build_envelope

from scripts.checks.data.audit import (
    APPLICATION,
    DEPLOYMENT,
    NATS_POD,
    REQUEST,
    RUN,
    TENANT,
    check,
    clean_up_database,
    clean_up_events,
    deploy,
    kubectl,
    nats_client,
    psql,
    request_record,
)
from scripts.setup.gateway_routes import port_forward
from scripts.setup.nats_access import SECRETS_FILE as NATS_SECRETS
from scripts.setup.provision_postgres import NAMESPACE as DATA
from scripts.setup.provision_postgres import read_passwords

REPOSITORY = Path(__file__).resolve().parents[3]
OBSERVABILITY = "windops-observability"
NATS_STATEFULSET = "statefulset/windops-nats"
EVENTS_BOARD = (
    REPOSITORY / "infrastructure" / "monitoring" / "grafana" / "dashboards" / "events.json"
)
PLATFORM_BOARD = EVENTS_BOARD.with_name("platform-health.json")
# A failure's panels stay empty until one happens, so they are checked after provoking one.
FILLED_BY_FAILURES = "Dead letters by consumer and reason"


class Prometheus:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def query(self, expression: str) -> list[dict[str, Any]]:
        response = httpx.get(f"{self.base}/api/v1/query", params={"query": expression}, timeout=15)
        response.raise_for_status()
        result: list[dict[str, Any]] = response.json()["data"]["result"]
        return result

    def value(self, expression: str) -> float | None:
        result = self.query(expression)
        return max(float(item["value"][1]) for item in result) if result else None

    def alert_states(self, name: str) -> list[str]:
        response = httpx.get(f"{self.base}/api/v1/alerts", timeout=15)
        response.raise_for_status()
        alerts = response.json()["data"]["alerts"]
        return [item["state"] for item in alerts if item["labels"]["alertname"] == name]


def wait_until(description: str, condition: Callable[[], bool], *, seconds: int = 240) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            print(f"ok - {description}", flush=True)
            return
        time.sleep(5)
    raise RuntimeError(f"Failed: {description}")


def apply_stack() -> None:
    kubectl("apply", "--kustomize", str(REPOSITORY / "infrastructure/kubernetes/base/data"))
    kubectl(
        "apply", "--kustomize", str(REPOSITORY / "infrastructure/kubernetes/base/observability")
    )
    for workload, namespace in (
        (NATS_STATEFULSET, DATA),
        ("statefulset/windops-prometheus", OBSERVABILITY),
        ("deployment/windops-grafana", OBSERVABILITY),
    ):
        kubectl("rollout", "restart", workload, f"--namespace={namespace}")
        kubectl("rollout", "status", workload, f"--namespace={namespace}", "--timeout=300s")
    kubectl(
        "wait", f"pod/{NATS_POD}", "--for=condition=Ready", f"--namespace={DATA}", "--timeout=180s"
    )


def board_queries(path: Path) -> dict[str, str]:
    board = json.loads(path.read_text())
    return {panel["title"]: panel["targets"][0]["expr"] for panel in board["panels"]}


def has_data(prometheus: Prometheus, expression: str) -> Callable[[], bool]:
    return lambda: bool(prometheus.query(expression))


def check_panels(prometheus: Prometheus) -> None:
    for title, expression in board_queries(EVENTS_BOARD).items():
        if title != FILLED_BY_FAILURES:
            wait_until(f"Events panel has data: {title}", has_data(prometheus, expression))
    platform = board_queries(PLATFORM_BOARD)
    ready = platform["Database, Redis, Qdrant and JetStream replicas ready"]
    wait_until(
        "Platform Health shows all four data stores ready",
        lambda: (
            {item["metric"].get("statefulset") for item in prometheus.query(ready)}
            >= {"windops-postgres", "windops-qdrant", "windops-nats", "windops-redis"}
        ),
    )
    for title in ("Data store memory", "Data store restarts in the last hour"):
        expression = platform[title]
        wait_until(
            f"Platform Health panel has data: {title}",
            has_data(prometheus, expression),
        )


async def publish_invalid(port: int, passwords: dict[str, str]) -> None:
    """A request claiming another service's name, which the consumer must dead-letter."""
    alarm = await nats_client(port, passwords, "alarm")
    envelope = build_envelope(
        audit_request_subject("approval"),
        tenant_id=TENANT,
        payload=REQUEST,
        producer="approval",
        event_id=f"events-check-claimed-{RUN}",
    )
    await alarm.jetstream().publish(
        audit_request_subject("alarm"),
        envelope.SerializeToString(),
        headers={"Nats-Msg-Id": envelope.event_id},
    )
    await alarm.close()


def check_dead_letter(prometheus: Prometheus, passwords: dict[str, str]) -> None:
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        asyncio.run(publish_invalid(port, passwords))
    expression = board_queries(EVENTS_BOARD)[FILLED_BY_FAILURES]
    wait_until(
        "a dead letter shows by consumer and reason",
        lambda: any(
            item["metric"].get("consumer") == "audit" and item["metric"].get("reason") == "invalid"
            for item in prometheus.query(expression)
        ),
    )
    wait_until(
        "WindopsDeadLetterMessages fires (EVT-003)",
        lambda: "firing" in prometheus.alert_states("WindopsDeadLetterMessages"),
    )
    with port_forward("service/windops-alertmanager", OBSERVABILITY, 9093) as port:
        alerts = httpx.get(f"http://127.0.0.1:{port}/api/v2/alerts", timeout=15).json()
    names = {item["labels"]["alertname"] for item in alerts}
    check("WindopsDeadLetterMessages" in names, "that alert reaches Alertmanager")
    output = subprocess.run(
        ["make", "--no-print-directory", "dead-letters"],
        cwd=REPOSITORY, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    check("reason=invalid" in output and "consumer=audit" in output, "make dead-letters lists it")
    check(TENANT not in output, "make dead-letters does not show message contents (OBS-003)")


def check_set_aside(prometheus: Prometheus) -> None:
    result = psql(
        "INSERT INTO outbox_events (event_id, subject, tenant_id, envelope) "  # noqa: S608
        f"VALUES ('unreadable-{RUN}', 'audit.recorded', '{TENANT}', '\\xffff')"
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not add the test row: {result.stderr}")
    wait_until(
        "an unreadable outbox row shows on the dashboard",
        lambda: (prometheus.value("max(windops_outbox_failed)") or 0) >= 1,
    )
    wait_until(
        "WindopsOutboxRowsSetAside fires",
        lambda: "firing" in prometheus.alert_states("WindopsOutboxRowsSetAside"),
    )


def check_consumer_lag(prometheus: Prometheus, passwords: dict[str, str]) -> None:
    expression = 'max(jetstream_consumer_num_pending{consumer_name="audit"})'
    kubectl("scale", DEPLOYMENT, "--replicas=0", f"--namespace={APPLICATION}")
    kubectl(
        "wait", "pod", "--selector=app.kubernetes.io/name=windops-audit", "--for=delete",
        f"--namespace={APPLICATION}", "--timeout=120s",
    )  # fmt: skip

    async def publish(port: int) -> None:
        alarm = await nats_client(port, passwords, "alarm")
        for index in range(3):
            await request_record(alarm, event_id=f"events-check-lag-{RUN}-{index}")
        await alarm.close()

    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        asyncio.run(publish(port))
    wait_until(
        "messages waiting for a stopped consumer show as lag (SRS 10.3)",
        lambda: (prometheus.value(expression) or 0) >= 3,
    )
    kubectl("scale", DEPLOYMENT, "--replicas=1", f"--namespace={APPLICATION}")
    kubectl("rollout", "status", DEPLOYMENT, f"--namespace={APPLICATION}", "--timeout=240s")
    wait_until(
        "the lag clears once the consumer returns", lambda: prometheus.value(expression) == 0
    )


def check_backlog_and_store_down(prometheus: Prometheus) -> None:
    kubectl("scale", NATS_STATEFULSET, "--replicas=0", f"--namespace={DATA}")
    kubectl("wait", f"pod/{NATS_POD}", "--for=delete", f"--namespace={DATA}", "--timeout=120s")
    envelope = build_envelope(
        "audit.recorded",
        tenant_id=TENANT,
        payload=events_pb2.AuditRecorded(
            audit_record_id=f"events-check-{RUN}", record_hash="check"
        ),
        producer="audit",
        event_id=f"backlog-{RUN}",
    )
    result = psql(
        "INSERT INTO outbox_events (event_id, subject, tenant_id, envelope) VALUES "  # noqa: S608
        f"('backlog-{RUN}', 'audit.recorded', '{TENANT}', "
        f"'\\x{envelope.SerializeToString().hex()}')"
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not add the test row: {result.stderr}")
    wait_until(
        "an event waiting out a JetStream outage shows as backlog (SRS 7.7)",
        lambda: (
            (prometheus.value("max(windops_outbox_waiting)") or 0) >= 1
            and (prometheus.value("max(windops_outbox_oldest_waiting_seconds)") or 0) > 0
        ),
    )
    wait_until(
        "WindopsDataStoreDown fires",
        lambda: "firing" in prometheus.alert_states("WindopsDataStoreDown"),
        seconds=300,
    )
    kubectl("scale", NATS_STATEFULSET, "--replicas=1", f"--namespace={DATA}")
    kubectl("rollout", "status", NATS_STATEFULSET, f"--namespace={DATA}", "--timeout=180s")
    wait_until(
        "the backlog clears once JetStream returns",
        lambda: prometheus.value("max(windops_outbox_waiting)") == 0,
    )


def main() -> int:
    passwords = read_passwords(NATS_SECRETS)
    code = 0
    try:
        apply_stack()
        deploy()
        with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as port:
            prometheus = Prometheus(port)
            check_panels(prometheus)
            check_dead_letter(prometheus, passwords)
            check_set_aside(prometheus)
            check_consumer_lag(prometheus, passwords)
            check_backlog_and_store_down(prometheus)
    except (RuntimeError, OSError, NatsError, TimeoutError, httpx.HTTPError,
            subprocess.CalledProcessError) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        code = 1
    finally:
        kubectl("scale", NATS_STATEFULSET, "--replicas=1", f"--namespace={DATA}", required=False)
        kubectl("scale", DEPLOYMENT, "--replicas=1", f"--namespace={APPLICATION}", required=False)
        kubectl(
            "rollout",
            "status",
            NATS_STATEFULSET,
            f"--namespace={DATA}",
            "--timeout=180s",
            required=False,
        )
        clean_up_database()
        with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
            asyncio.run(clean_up_events(port, passwords))
    if code == 0:
        print("Events check passed")
    return code


if __name__ == "__main__":
    sys.exit(main())
