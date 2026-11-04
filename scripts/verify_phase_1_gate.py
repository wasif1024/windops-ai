#!/usr/bin/env python3
"""Check the Phase 1 exit gate on the real local cluster.

SRS section 12: a sample gRPC call is traced through the gateway, and its breaker opens under an
injected fault. Run `make cluster` first. The system is left running and healthy afterwards.
"""

import secrets
import time
from typing import Any

import httpx
from gateway_routes import port_forward
from verify_cluster import kubectl, run
from verify_observability import prometheus_query, wait_until

SYSTEM = "windops-system"
APPLICATION = "windops-application"
OBSERVABILITY = "windops-observability"
RESPONDER = "deployment/windops-sample-responder"
BREAKER_STATE = (
    'max(windops_circuit_breaker_state{workload="windops-sample-caller",'
    'dependency="sample-responder"})'
)
WORKLOADS = (
    ("deployment/windops-sample-responder", APPLICATION),
    ("deployment/windops-sample-caller", APPLICATION),
    ("deployment/windops-gateway", SYSTEM),
    ("deployment/windops-otel-collector", OBSERVABILITY),
    ("statefulset/windops-tempo", OBSERVABILITY),
    ("statefulset/windops-prometheus", OBSERVABILITY),
    ("statefulset/windops-loki", OBSERVABILITY),
    ("daemonset/windops-fluent-bit", OBSERVABILITY),
)


def bootstrap() -> None:
    for target in ("images", "secrets", "infra", "services"):
        run("make", target)
    # A ConfigMap change does not restart the pods that read it.
    kubectl("rollout", "restart", "deployment/windops-gateway", f"--namespace={SYSTEM}")
    kubectl(
        "rollout", "restart", "deployment/windops-otel-collector",
        f"--namespace={OBSERVABILITY}",
    )  # fmt: skip
    kubectl(
        "rollout", "status", "deployment/windops-otel-collector",
        f"--namespace={OBSERVABILITY}", "--timeout=180s",
    )  # fmt: skip
    for workload, namespace in WORKLOADS:
        kubectl("rollout", "status", workload, f"--namespace={namespace}", "--timeout=300s")
    run("make", "gateway-routes")


def spans_by_service(trace: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for batch in trace["batches"]:
        attributes = batch["resource"]["attributes"]
        service = next(
            item["value"]["stringValue"] for item in attributes if item["key"] == "service.name"
        )
        for scope in batch.get("scopeSpans", []):
            name = scope.get("scope", {}).get("name", "")
            found.setdefault(service, []).extend({**span, "scope": name} for span in scope["spans"])
    return found


def span_id(span: dict[str, Any]) -> str:
    return str(span["spanId"])


def check_trace_through_gateway(gateway_port: int) -> str:
    trace_id = secrets.token_hex(16)
    response = httpx.get(
        f"http://127.0.0.1:{gateway_port}/sample/echo",
        params={"message": "traced"},
        headers={"traceparent": f"00-{trace_id}-b7ad6b7169203331-01"},
        timeout=15,
    )
    if response.status_code != 200 or response.json()["tenant_id"] != "sample":
        raise RuntimeError(f"The traced call failed: {response.status_code} {response.text}")

    with port_forward("service/windops-tempo", OBSERVABILITY, 3200) as tempo_port:

        def complete() -> dict[str, list[dict[str, Any]]] | None:
            reply = httpx.get(f"http://127.0.0.1:{tempo_port}/api/traces/{trace_id}", timeout=10)
            if reply.status_code != 200:
                return None
            services = spans_by_service(reply.json())
            needed = {"windops-gateway", "sample-caller", "sample-responder"}
            return services if needed <= set(services) else None

        services = wait_until("Tempo has spans from the gateway and both services", complete)

    gateway_ids = {span_id(span) for span in services["windops-gateway"]}
    caller = services["sample-caller"]
    # FastAPI adds its own span with the same name, so select this code's span by its source.
    http_span = next(
        span
        for span in caller
        if span["name"] == "GET /sample/echo" and span["scope"] == "windops.sample"
    )
    client_span = next(span for span in caller if span["name"] == "call sample-responder")
    server_span = next(
        span for span in services["sample-responder"] if span["name"].endswith("/Echo")
    )
    chain = (
        ("the caller's HTTP span", http_span.get("parentSpanId"), gateway_ids),
        ("the caller's client span", client_span.get("parentSpanId"), {span_id(http_span)}),
        ("the responder's server span", server_span.get("parentSpanId"), {span_id(client_span)}),
    )
    for name, parent, expected in chain:
        if parent not in expected:
            raise RuntimeError(f"{name} has the wrong parent: {parent}, expected {expected}")
    status = {
        item["key"]: item["value"].get("stringValue") for item in server_span["attributes"]
    }.get("rpc.grpc.status")
    if status != "OK":
        raise RuntimeError(f"The gRPC call's span has status {status}")
    print(f"Trace {trace_id}: gateway, then caller, then client, then responder, status OK")
    return trace_id


def check_tempo_survives_restart(trace_id: str) -> None:
    """Tempo writes stored traces out when it restarts, which needs far more memory than idling."""
    kubectl(
        "delete", "pod", "windops-tempo-0", f"--namespace={OBSERVABILITY}", "--wait=true",
    )  # fmt: skip
    kubectl(
        "rollout", "status", "statefulset/windops-tempo", f"--namespace={OBSERVABILITY}",
        "--timeout=180s",
    )  # fmt: skip
    with port_forward("service/windops-tempo", OBSERVABILITY, 3200) as tempo_port:

        def still_there() -> bool:
            url = f"http://127.0.0.1:{tempo_port}/api/traces/{trace_id}"
            return httpx.get(url, timeout=10).status_code == 200

        wait_until("Tempo restarted while holding the trace, and still has it", still_there)
    # Writing a block happens seconds after startup, so check again that it survived that.
    time.sleep(45)
    status = kubectl(
        "get", "pod", "windops-tempo-0", f"--namespace={OBSERVABILITY}",
        "--output=jsonpath={.status.containerStatuses[0].restartCount}",
    ).stdout.strip()  # fmt: skip
    if status != "0":
        raise RuntimeError(f"Tempo restarted {status} times after holding real traces")


def check_log_has_trace(trace_id: str) -> None:
    query = f'{{namespace="{APPLICATION}"}} |= "{trace_id}"'
    with port_forward("service/windops-loki", OBSERVABILITY, 3100) as port:

        def found() -> bool:
            reply = httpx.get(
                f"http://127.0.0.1:{port}/loki/api/v1/query_range",
                params={"query": query, "since": "15m", "limit": 20},
                timeout=10,
            )
            pods = {stream["stream"].get("pod", "") for stream in reply.json()["data"]["result"]}
            return any("sample-responder" in pod for pod in pods)

        wait_until("Loki has the responder's log line, with the same trace ID", found)


def call(gateway_port: int) -> tuple[int, dict[str, Any], float]:
    started = time.monotonic()
    response = httpx.get(
        f"http://127.0.0.1:{gateway_port}/sample/echo",
        params={"message": "fault"},
        timeout=15,
    )
    return response.status_code, response.json(), time.monotonic() - started


def restart_count(name: str) -> list[str]:
    output = kubectl(
        "get", "pods", f"--namespace={APPLICATION}",
        f"--selector=app.kubernetes.io/name={name}",
        "--output=jsonpath={.items[*].status.containerStatuses[0].restartCount}",
    ).stdout  # fmt: skip
    return output.split()


def wait_for_breaker_state(prometheus_port: int, expected: float) -> None:
    def matches() -> bool:
        result = prometheus_query(prometheus_port, BREAKER_STATE)
        return bool(result) and float(result[0]["value"][1]) == expected

    names = {0.0: "closed", 2.0: "open"}
    wait_until(f"Prometheus shows the breaker as {names[expected]}", matches, attempts=36)


def check_breaker_under_fault(gateway_port: int) -> None:
    caller_restarts = restart_count("windops-sample-caller")
    kubectl("scale", RESPONDER, f"--namespace={APPLICATION}", "--replicas=0")
    print("Injected the fault: the responder is scaled to zero")
    try:
        kubectl(
            "wait", "--for=delete", "pod", f"--namespace={APPLICATION}",
            "--selector=app.kubernetes.io/name=windops-sample-responder", "--timeout=60s",
        )  # fmt: skip
        reasons = []
        for _ in range(30):
            status, body, _ = call(gateway_port)
            if status != 503:
                raise RuntimeError(f"Expected 503 during the fault, not {status}: {body}")
            reasons.append(body["reason"])
            if body["reason"] == "breaker_open":
                break
        else:
            raise RuntimeError(f"The breaker never opened: {reasons}")
        print(f"The breaker opened after {reasons.count('call_failed')} failed requests")

        timings = [call(gateway_port) for _ in range(5)]
        if any(reply[1]["reason"] != "breaker_open" for reply in timings):
            raise RuntimeError(f"The breaker did not stay open: {timings}")
        slowest = max(reply[2] for reply in timings)
        if slowest >= 1:
            raise RuntimeError(f"An open breaker should fail at once, but took {slowest:.2f}s")
        print(f"While open, requests fail fast: the slowest of 5 took {slowest * 1000:.0f} ms")

        with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as prometheus_port:
            wait_for_breaker_state(prometheus_port, 2.0)

            def alert_active() -> bool:
                reply = httpx.get(f"http://127.0.0.1:{prometheus_port}/api/v1/alerts", timeout=10)
                return any(
                    alert["labels"]["alertname"] == "WindopsCircuitBreakerOpen"
                    for alert in reply.json()["data"]["alerts"]
                )

            wait_until("The WindopsCircuitBreakerOpen alert is active", alert_active, attempts=24)
        check_breaker_logged()
        if restart_count("windops-sample-caller") != caller_restarts:
            raise RuntimeError("The caller restarted during the fault, which RES-006 forbids")
        print("The caller stayed up during the fault, with no restart")
    finally:
        kubectl("scale", RESPONDER, f"--namespace={APPLICATION}", "--replicas=1")
    kubectl("rollout", "status", RESPONDER, f"--namespace={APPLICATION}", "--timeout=180s")


def check_breaker_logged() -> None:
    query = f'{{namespace="{APPLICATION}"}} |= "Circuit breaker state changed" |= "open"'
    with port_forward("service/windops-loki", OBSERVABILITY, 3100) as port:

        def found() -> bool:
            reply = httpx.get(
                f"http://127.0.0.1:{port}/loki/api/v1/query_range",
                params={"query": query, "since": "15m", "limit": 5},
                timeout=10,
            )
            return bool(reply.json()["data"]["result"])

        wait_until("Loki has the breaker's state change", found)


def check_recovery(gateway_port: int) -> None:
    started = time.monotonic()

    def recovered() -> bool:
        return call(gateway_port)[0] == 200

    wait_until(
        "The breaker let probes through, and calls succeed again", recovered, attempts=45, delay=2
    )
    print(f"Recovered {time.monotonic() - started:.0f}s after the responder returned")
    # A half-open breaker needs 3 successful probes to close, and one has just succeeded.
    for _ in range(2):
        status, body, _ = call(gateway_port)
        if status != 200:
            raise RuntimeError(f"A probe failed after recovery: {status} {body}")
    with port_forward("service/windops-prometheus", OBSERVABILITY, 9090) as prometheus_port:
        wait_for_breaker_state(prometheus_port, 0.0)


def main() -> int:
    bootstrap()
    with port_forward("service/windops-gateway", SYSTEM, 80) as gateway_port:
        wait_until(
            "The gateway reaches the sample services",
            lambda: call(gateway_port)[0] == 200,
            attempts=30,
            delay=2,
        )
        trace_id = check_trace_through_gateway(gateway_port)
        check_log_has_trace(trace_id)
        check_tempo_survives_restart(trace_id)
        check_breaker_under_fault(gateway_port)
        check_recovery(gateway_port)
    print("The Phase 1 exit gate passes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
