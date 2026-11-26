#!/usr/bin/env python3
"""Prove the polled stale rules on the cluster: condition drift and weather (M10.3b, SRS 8.5).

**Drift**, in the `asset-check` tenant:
- the check makes a turbine of its own, with a device credential, and sends its telemetry through
  the gateway as a device would, its vibration steady at 2 mm/s;
- a Tenant Administrator publishes a check-only fault rule on that vibration, at medium severity;
- the detector opens a detection, which opens a case, whose evidence freezes the figure, 2;
- the vibration rises to 6, the rule's figure with it, and the watcher marks the case stale by
  the drift rule, naming the rule and both figures. The severity never changed.

**Weather**, in `north-sea`, with the real Policy Service:
- an engineer opens a case on a demo turbine, and a planner submits work at height in a window
  the forecast allows;
- a Tenant Administrator publishes a check-only work-window rule that no wind meets;
- asked again, Policy blocks the window it passed, and the watcher marks the case stale by the
  weather rule; the Maintenance Service marks the work order stale with it.

The watcher looks every five minutes, so this takes up to 15 minutes. Run `make services` and
`make gateway-routes` first. Needs local port 9443 free. What it makes is removed at the end.
"""

import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from google.protobuf.timestamp_pb2 import Timestamp
from windops.asset.v1 import asset_pb2, asset_pb2_grpc
from windops.fault_detection.v1 import fault_detection_pb2 as fd
from windops.fault_detection.v1 import fault_detection_pb2_grpc
from windops.investigation.v1 import investigation_pb2 as inv
from windops.investigation.v1 import investigation_pb2_grpc
from windops.maintenance.v1 import maintenance_pb2 as mnt
from windops.maintenance.v1 import maintenance_pb2_grpc
from windops.policy.v1 import policy_pb2 as pol
from windops.policy.v1 import policy_pb2_grpc
from windops.weather.v1 import weather_pb2, weather_pb2_grpc

from scripts.checks.agents.tools import INVESTIGATION_HOST, Cases
from scripts.checks.fleet import approval as approval_check
from scripts.checks.fleet import fault_rules
from scripts.checks.fleet import policy as policy_check
from scripts.checks.fleet.asset import HOST as ASSET_HOST
from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.data_quality import SAMPLES, device_token
from scripts.checks.fleet.detection import HOST as FAULT_DETECTION_HOST
from scripts.checks.fleet.maintenance import HOST as MAINTENANCE_HOST
from scripts.checks.fleet.telemetry import as_tenant
from scripts.checks.fleet.weather import HOST as WEATHER_HOST
from scripts.lib.settings import KUBE_CONTEXT
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

RUN = str(int(time.time()))
PREFIX = "stale-watch-check-"
# Drift: a turbine of the check's own, in a tenant for checks.
DRIFT_TENANT, FARM, TURBINE = "asset-check", "stale-watch-farm", "stale-watch-t01"
DRIFT_ADMIN, DRIFT_ENGINEER = f"{PREFIX}admin", f"{PREFIX}engineer"
FAULT_RULE = f"{PREFIX}vibration-{RUN}"
# Weather: a demo turbine, and a work-window rule no wind meets.
TENANT, DEMO_TURBINE = "north-sea", "dogger-east-t01"
ADMIN, ENGINEER, PLANNER = f"{PREFIX}admin", f"{PREFIX}engineer", f"{PREFIX}planner"
POLICY_RULE = f"{PREFIX}wind-{RUN}"
WATCH_SECONDS = 15 * 60


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def until(what: str, probe: Callable[[], Any], timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        time.sleep(5)
    raise RuntimeError(f"Timed out waiting for {what}")


def stamp(value: datetime) -> Timestamp:
    made = Timestamp()
    made.FromDatetime(value)
    return made


class Device(threading.Thread):
    """Sends the check turbine's telemetry, a sample a second, its vibration as set."""

    def __init__(self, token: str) -> None:
        super().__init__(daemon=True)
        self.token, self.vibration, self.stop, self.sent = token, 2.0, threading.Event(), 0
        self.error = ""

    def sample(self, sequence: int) -> dict[str, Any]:
        values = dict.fromkeys(
            ("wind_speed_mps", "wind_direction_deg", "rotor_speed_rpm", "generator_speed_rpm",
             "pitch_angle_deg", "ambient_temp_c", "gearbox_oil_temp_c", "gearbox_bearing_temp_c",
             "main_bearing_temp_c", "generator_bearing_temp_c"),
            10.0,
        )  # fmt: skip
        return {
            "schema_version": "2", "tenant_id": DRIFT_TENANT, "turbine_id": TURBINE,
            "event_time": datetime.fromtimestamp(sequence, UTC).isoformat(),
            "sequence": sequence, "source": "stale-watch-check", "model_version": "check",
            "synthetic": True, "operating_state": "producing", "available": True,
            "power_kw": 1000.0, "nacelle_vibration_mm_s": self.vibration, **values,
        }  # fmt: skip

    def run(self) -> None:
        # A sample a second, as the rules need nine in ten seconds covered, sent five at a time.
        last = int(time.time()) - 1
        while not self.stop.is_set():
            now = int(time.time())
            batch = [self.sample(second) for second in range(last + 1, now + 1)]
            last = now
            try:
                answer = httpx.post(
                    SAMPLES, json={"samples": batch}, timeout=15,
                    headers={"Authorization": f"Bearer {self.token}"},
                )  # fmt: skip
                if answer.status_code == 200:
                    self.sent += len(batch)
                else:
                    self.error = f"{answer.status_code}: {answer.text[:200]}"
            except httpx.HTTPError as error:
                self.error = type(error).__name__
            self.stop.wait(5)


class Bench:
    def __init__(self, stack: ExitStack, key: str) -> None:
        bundle = client_bundle()

        def channel(deployment: str, host: str) -> Any:
            port = stack.enter_context(
                port_forward(f"deployment/{deployment}", "windops-application", 50051)
            )
            return stack.enter_context(blocking_channel(port, host, bundle))

        self.assets = Caller(asset_pb2_grpc.AssetServiceStub(
            channel("windops-asset", ASSET_HOST)), key)  # fmt: skip
        self.rules = Caller(fault_detection_pb2_grpc.FaultRuleServiceStub(
            channel("windops-fault-detection", FAULT_DETECTION_HOST)), key)  # fmt: skip
        self.investigation = Caller(investigation_pb2_grpc.InvestigationServiceStub(
            channel("windops-investigation", INVESTIGATION_HOST)), key)  # fmt: skip
        self.maintenance = Caller(maintenance_pb2_grpc.MaintenanceServiceStub(
            channel("windops-maintenance", MAINTENANCE_HOST)), key)  # fmt: skip
        self.weather = Caller(weather_pb2_grpc.WeatherServiceStub(
            channel("windops-weather", WEATHER_HOST)), key)  # fmt: skip
        self.policy = Caller(policy_pb2_grpc.PolicyServiceStub(
            channel("windops-policy", policy_check.HOST)), key)  # fmt: skip
        self.cases = Cases(key)
        stack.callback(self.cases.close)
        self.drift_case = ""

    def case(self, case_id: str, user: str = ENGINEER, tenant: str = TENANT) -> Any:
        return self.investigation.call("GetCase", inv.GetCaseRequest(case_id=case_id), user,
                                       tenant).case  # fmt: skip


def make_turbine(bench: Bench) -> None:
    """The check's own turbine, or the one from before."""
    for method, request in (
        ("CreateWindFarm", asset_pb2.CreateWindFarmRequest(
            wind_farm_id=FARM, name=FARM, country="GB", time_zone="Europe/London",
            location=asset_pb2.Location(latitude=54.0, longitude=1.0), offshore=True)),
        ("CreateTurbine", asset_pb2.CreateTurbineRequest(
            turbine_id=TURBINE, wind_farm_id=FARM, name="Stale watch check T01",
            location=asset_pb2.Location(latitude=54.0, longitude=1.0),
            model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic", model="check"))),
    ):  # fmt: skip
        code = bench.assets.code(method, request, DRIFT_ADMIN, DRIFT_TENANT)
        if code not in (grpc.StatusCode.OK, grpc.StatusCode.ALREADY_EXISTS):
            raise RuntimeError(f"{method} answered {code.name}")


def start_drift(bench: Bench, device: Device) -> None:
    """A detection case on the check turbine, its evidence holding the vibration at 2."""
    until("the device's telemetry", lambda: device.sent >= 3, 60)
    if device.error:
        raise RuntimeError(f"The device route failed: {device.error}")
    definition = {"kind": "threshold",
                  "parameters": {"metric": "nacelle_vibration_mm_s", "for_seconds": 60},
                  "levels": [{"at": 1, "severity": "medium"}]}  # fmt: skip
    bench.rules.call("PublishFaultRuleVersion", fd.PublishFaultRuleVersionRequest(
        rule_id=FAULT_RULE, name="Stale watch check vibration", reason="The stale watch check",
        definition_json=json.dumps(definition)), DRIFT_ADMIN, DRIFT_TENANT)  # fmt: skip

    def opened() -> Any:
        fault_rules.last_line(fault_rules.sql(
            "UPDATE turbine_watch SET last_evaluated_at = NULL "  # noqa: S608
            f"WHERE turbine_id = '{TURBINE}'; SELECT 1", tenant=DRIFT_TENANT,
        ))  # fmt: skip
        listed = bench.investigation.call(
            "ListCases", inv.ListCasesRequest(turbine_id=TURBINE), DRIFT_ENGINEER, DRIFT_TENANT
        ).cases
        return next((c for c in listed if c.evidence_snapshot_id), None)

    case = until("a case with frozen evidence on the check turbine", opened, 300)
    bench.drift_case = case.case_id
    request = inv.GetEvidenceSnapshotRequest(snapshot_id=case.evidence_snapshot_id)
    snapshot = bench.investigation.call("GetEvidenceSnapshot", request, DRIFT_ENGINEER,
                                        DRIFT_TENANT).snapshot  # fmt: skip
    frozen = [json.loads(i.content_json) for i in snapshot.items if i.record_type == "detection"]
    figures = [d.get("amount", 0.0) for d in frozen if d.get("rule_id") == FAULT_RULE]
    check(bool(figures) and all(abs(f - 2.0) < 0.01 for f in figures),
          f"The detector opened case {case.case_id}, its evidence freezing 2 mm/s")  # fmt: skip
    device.vibration = 6.0
    print("info - The vibration rises to 6 mm/s, at the same severity", flush=True)


def calm_window(bench: Bench) -> datetime:
    """The first three hours in the next week the forecast keeps well inside the climbing
    limits, so the submission never rests on today's wind."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    request = weather_pb2.GetForecastRequest(
        turbine_id=DEMO_TURBINE, from_time=stamp(now + timedelta(hours=2)),
        to_time=stamp(now + timedelta(days=6)),
    )  # fmt: skip
    points = list(bench.weather.call("GetForecast", request, ENGINEER, TENANT).points)

    def calm(point: Any) -> bool:
        if not point.HasField("wind_speed_hub_mps") or not point.HasField("wind_gusts_10m_mps"):
            return False
        return bool(point.wind_speed_hub_mps < 9.0 and point.wind_gusts_10m_mps < 14.0)

    for first in range(len(points) - 4):
        if all(calm(p) for p in points[first : first + 5]):
            start: datetime = points[first + 1].valid_at.ToDatetime(tzinfo=UTC)
            print(f"info - The forecast is calm from {start:%Y-%m-%d %H:%M} UTC", flush=True)
            return start
    raise RuntimeError("The forecast has no calm three hours in the next week to plan work in")


def start_weather(bench: Bench) -> tuple[str, str]:
    """A case on a demo turbine, its work at height submitted, then a rule no wind meets."""
    case_id = bench.cases.open(ENGINEER, title=f"Stale watch check {RUN}")
    bench.cases.evidence(case_id)
    start = calm_window(bench)
    plan = mnt.Plan(
        title=f"Stale watch check {RUN}: inspect the nacelle", action="inspection",
        summary="Climb and inspect the gearbox oil cooler.",
        tasks=[mnt.Task(description="Inspect the oil cooler in the nacelle", duration_hours=2,
                        crew_size=2, requires_climb=True)],
        planned_start=stamp(start), planned_end=stamp(start + timedelta(hours=3)),
        estimated_downtime_hours=3,
    )  # fmt: skip
    drafting = mnt.CreateDraftRequest(case_id=case_id, plan=plan, idempotency_key=f"swc-{RUN}")
    draft = bench.maintenance.call("CreateDraft", drafting, PLANNER, TENANT).work_order
    order = bench.maintenance.call("SubmitWorkOrder", mnt.SubmitWorkOrderRequest(
        work_order_id=draft.work_order_id, expected_revision=draft.revision,
        idempotency_key=f"swc-{RUN}-submit"), PLANNER, TENANT).work_order  # fmt: skip
    check(order.status == "submitted" and order.validation.policy_outcome != "block",
          f"Work at height is submitted as {order.work_order_id}: the forecast allows its "
          f"window ({order.validation.policy_outcome})")  # fmt: skip
    definition = {
        "kind": "limit", "category": "weather_window",
        "parameters": {"input": "weather.max_wind_speed_mps", "above": 0.0,
                       "when": {"input": "proposal.requires_climb", "equals": True},
                       "max_age_seconds": 3 * 3600},
        "outcome": "block", "when_missing": "block",
    }  # fmt: skip
    bench.policy.call("PublishPolicyRuleVersion", pol.PublishPolicyRuleVersionRequest(
        rule_id=POLICY_RULE, name="Stale watch check wind", definition_json=json.dumps(definition),
        reason="The stale watch check: no wind meets it", expected_version=0,
        idempotency_key=f"swc-{RUN}-rule"), ADMIN, TENANT)  # fmt: skip
    print(f"info - A work-window rule no wind meets, {POLICY_RULE}, is published", flush=True)
    return case_id, order.work_order_id


def prove(bench: Bench, device: Device) -> None:
    start_drift(bench, device)
    weather_case, order_id = start_weather(bench)

    drifted = until("the drift rule",
                    lambda: (c := bench.case(bench.drift_case, DRIFT_ENGINEER, DRIFT_TENANT))
                    .stale_rule and c, WATCH_SECONDS)  # fmt: skip
    check(drifted.stale_rule == "drift" and FAULT_RULE in drifted.stale_detail
          and drifted.severity == "medium",
          f"The case goes stale by drift, at one severity: {drifted.stale_detail}")  # fmt: skip

    weathered = until("the weather rule",
                      lambda: (c := bench.case(weather_case)).stale_rule and c,
                      WATCH_SECONDS)  # fmt: skip
    check(weathered.stale_rule == "weather" and POLICY_RULE in weathered.stale_detail
          and weathered.stale_rules_version == "stale-rules-2",
          f"The other goes stale by weather: {weathered.stale_detail[:160]}")  # fmt: skip
    order = until("the work order to be stale", lambda: (w := bench.maintenance.call(
        "GetWorkOrder", mnt.GetWorkOrderRequest(work_order_id=order_id), PLANNER, TENANT
    ).work_order).status == "stale" and w, 120)  # fmt: skip
    check(
        order.stale_rule == "weather",
        "Its submitted work order is stale with it, and cannot be approved (EVD-007)",
    )


def cancel_drift_cases(bench: Bench) -> None:
    """Every active case on the check turbine, so a run never finds one from before."""
    listed = bench.investigation.call("ListCases", inv.ListCasesRequest(turbine_id=TURBINE),
                                      DRIFT_ENGINEER, DRIFT_TENANT).cases  # fmt: skip
    for case in listed:
        cancel = inv.CancelCaseRequest(case_id=case.case_id, reason="Check over")
        bench.investigation.code("CancelCase", cancel, DRIFT_ENGINEER, DRIFT_TENANT)


def clean_up(bench: Bench | None, device: Device | None) -> None:
    if device is not None:
        device.stop.set()
    if bench is not None:
        cancel_drift_cases(bench)
    if not re.fullmatch(r"[a-z-]+", PREFIX):
        raise ValueError("A fixed prefix of letters and hyphens")
    fault_rules.last_line(fault_rules.sql(
        "ALTER TABLE fault_rule_versions DISABLE TRIGGER fault_rule_versions_never_change; "  # noqa: S608
        f"DELETE FROM fault_rule_versions WHERE rule_id LIKE '{PREFIX}%'; "
        f"DELETE FROM fault_rules WHERE rule_id LIKE '{PREFIX}%'; "
        f"DELETE FROM detections WHERE rule_id LIKE '{PREFIX}%'; "
        "ALTER TABLE fault_rule_versions ENABLE TRIGGER fault_rule_versions_never_change; SELECT 1"
    ))  # fmt: skip
    policy_check.sql(
        "ALTER TABLE policy_rule_versions DISABLE TRIGGER policy_rule_versions_never_change; "  # noqa: S608
        f"DELETE FROM policy_rule_versions WHERE rule_id LIKE '{PREFIX}%'; "
        f"DELETE FROM policy_rules WHERE rule_id LIKE '{PREFIX}%'; "
        f"DELETE FROM idempotency_keys WHERE user_id LIKE '{PREFIX}%'; "
        "ALTER TABLE policy_rule_versions ENABLE TRIGGER policy_rule_versions_never_change; "
        "SELECT 1"
    )  # fmt: skip
    mine = f"tenant_id = '{DRIFT_TENANT}' AND turbine_id = '{TURBINE}'"
    as_tenant(DRIFT_TENANT, (
        f"DELETE FROM operational_samples WHERE {mine}; "  # noqa: S608
        f"UPDATE device_credentials SET revoked_at = now() WHERE {mine} AND revoked_at IS NULL; "
        "SELECT 1"
    ))  # fmt: skip
    if bench is not None:
        approval_check.clean_up(bench.cases.opened)


def main() -> int:
    bench: Bench | None = None
    device: Device | None = None
    try:
        key = signing_key()
        bootstrap(DRIFT_TENANT, DRIFT_ADMIN, "tenant-administrator")
        bootstrap(DRIFT_TENANT, DRIFT_ENGINEER, "reliability-engineer")
        bootstrap(TENANT, ADMIN, "tenant-administrator")
        bootstrap(TENANT, ENGINEER, "reliability-engineer")
        bootstrap(TENANT, PLANNER, "maintenance-planner")
        with ExitStack() as stack:
            stack.enter_context(keycloak_public())
            bench = Bench(stack, key)
            stack.callback(lambda: clean_up(bench, device))
            make_turbine(bench)
            cancel_drift_cases(bench)
            device = Device(device_token(DRIFT_TENANT, TURBINE))
            device.start()
            prove(bench, device)
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Stale watch check passed")
    return 0


if __name__ == "__main__":
    assert KUBE_CONTEXT  # noqa: S101
    sys.exit(main())
