#!/usr/bin/env python3
"""Prove alarms on the cluster: from a device, through the gateway, to people (FR-ALM-001 to 004).

Run the platform and the simulator first, as `make dev` does. Needs local port 9443 free. A demo
turbine's device sends a check-only alarm through the gateway's device route, as a controller
would, and the check proves:

- the Telemetry Service queues it as alarm.reported, and the Alarm Service makes it an active
  alarm, its severity from the versioned catalogue, as the device sent none (FR-ALM-004);
- a clear that arrives before its raise still ends the alarm it belongs to;
- an auditor may read it but not acknowledge it, and another tenant cannot find it;
- an operator acknowledges, shelves and unshelves it, each with who and when, and the device
  clears it (FR-ALM-002);
- every change is announced as alarm.changed (FR-ALM-003), and the acknowledgement is audited;
- row-level security hides the alarm from another tenant in PostgreSQL itself.

Its alarms have a tag of their own, and are removed at the end.
"""

import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from windops.alarm.v1 import alarm_pb2 as pb
from windops.alarm.v1 import alarm_pb2_grpc
from windops.audit.v1 import audit_pb2, audit_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import device_token, kubectl, send_to
from scripts.lib.tls_client import ClientBundle, blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

APPLICATION, DATA = "windops-application", "windops-data"
HOST = "windops-alarm.windops-application.svc.cluster.local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
DATABASE = "windops_alarm"
TENANT, OTHER, TURBINE = "north-sea", "atlas-wind", "dogger-east-t01"
OPERATOR, AUDITOR, STRANGER = "alarm-check-operator", "alarm-check-auditor", "alarm-check-other"
CODE = "ALARM_CHECK"
TAG, EARLY = f"alarm-check-{secrets.token_hex(3)}", f"alarm-check-{secrets.token_hex(3)}-early"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def sql(statement: str, tenant: str = "") -> str:
    """As the Alarm Service's own login, in one tenant, or across them all."""
    setting = (
        f"SELECT set_config('windops.tenant_id', '{tenant}', false); "
        if tenant
        else "SELECT set_config('windops.all_tenants', 'on', false); "
    )
    result = kubectl(
        "exec", "windops-postgres-0", "--", "psql", "--no-psqlrc", "--quiet", "--tuples-only",
        "--no-align", f"--username={DATABASE}", f"--dbname={DATABASE}", "--set=ON_ERROR_STOP=1",
        f"--command={setting}{statement}",
        namespace=DATA,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"SQL failed: {result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def report(action: str, tag: str, at: datetime) -> dict[str, Any]:
    return {
        "schema_version": "1", "tenant_id": TENANT, "turbine_id": TURBINE,
        "report_id": f"{CODE}:{tag}:{action}:{int(at.timestamp())}", "code": CODE, "tag": tag,
        "action": action, "event_time": at.isoformat(), "message": "Raised by the alarm check",
        "source": "alarm-check",
    }  # fmt: skip


def mine(alarms: Caller, tag: str, *states: Any) -> Any | None:
    request = pb.ListAlarmsRequest(turbine_id=TURBINE, states=list(states))
    found = [a for a in alarms.call("ListAlarms", request, OPERATOR, TENANT).alarms if a.tag == tag]
    return found[0] if found else None


def wait_for(alarms: Caller, tag: str, *states: Any) -> Any | None:
    for _ in range(40):
        found = mine(alarms, tag, *states)
        if found is not None:
            return found
        time.sleep(1)
    return None


def check_reported(alarms: Caller, token: str) -> Any:
    now = datetime.now(UTC).replace(microsecond=0)
    raised = report("raise", TAG, now - timedelta(seconds=5))
    with keycloak_public():
        answer = send_to("alarms", token, [raised, raised])
        check(
            answer.status_code == 200 and answer.json()["accepted"] == 1
            and answer.json()["duplicates"] == 1,
            "A device's alarm goes through the gateway's device route, and its repeat is kept once",
        )  # fmt: skip
        # The clear for another alarm, sent before that alarm's raise.
        send_to("alarms", token, [report("clear", EARLY, now - timedelta(seconds=1))])
        send_to("alarms", token, [report("raise", EARLY, now - timedelta(seconds=3))])
    alarm = wait_for(alarms, TAG)
    if alarm is None:
        raise RuntimeError("Failed: The Alarm Service made it an alarm, from alarm.reported")
    check(True, "The Alarm Service made it an alarm, from alarm.reported")
    check(
        alarm.state == pb.ALARM_STATE_ACTIVE and alarm.code == CODE
        and alarm.severity == pb.ALARM_SEVERITY_MEDIUM
        and alarm.severity_source.startswith("catalog:"),
        f"It is active, its severity from the catalogue as the device sent none "
        f"({alarm.severity_source})",
    )  # fmt: skip
    early = wait_for(alarms, EARLY, pb.ALARM_STATE_CLEARED)
    check(early is not None, "A clear that arrived before its raise ended that alarm at once")
    return alarm


def check_who_may_act(alarms: Caller, alarm: Any) -> None:
    get = pb.GetAlarmRequest(alarm_id=alarm.alarm_id)
    read = alarms.code("GetAlarm", get, AUDITOR, TENANT)
    ack = pb.AcknowledgeAlarmRequest(alarm_id=alarm.alarm_id)
    refused = alarms.code("AcknowledgeAlarm", ack, AUDITOR, TENANT)
    check(
        read == grpc.StatusCode.OK and refused == grpc.StatusCode.PERMISSION_DENIED,
        f"An auditor may read it, but not acknowledge it ({read.name}, {refused.name})",
    )
    hidden = alarms.code("GetAlarm", get, STRANGER, OTHER)
    check(hidden == grpc.StatusCode.NOT_FOUND, "Another tenant cannot find it")
    rows = sql(f"SELECT count(*) FROM alarms WHERE tag = '{TAG}'", tenant=OTHER)  # noqa: S608
    check(rows == "0", "and row-level security hides it from that tenant in PostgreSQL")


def check_operator(alarms: Caller, alarm: Any, token: str) -> None:
    key = secrets.token_hex(8)
    ack = pb.AcknowledgeAlarmRequest(alarm_id=alarm.alarm_id, comment="Seen", idempotency_key=key)
    first = alarms.call("AcknowledgeAlarm", ack, OPERATOR, TENANT).alarm
    again = alarms.call("AcknowledgeAlarm", ack, OPERATOR, TENANT).alarm
    check(
        first.state == pb.ALARM_STATE_ACKNOWLEDGED and first.acknowledged_by == OPERATOR
        and again == first,
        "An operator acknowledges it, and a retry with the same key changes nothing",
    )  # fmt: skip
    shelve = pb.ShelveAlarmRequest(alarm_id=alarm.alarm_id, reason="Alarm check", minutes=5)
    shelved = alarms.call("ShelveAlarm", shelve, OPERATOR, TENANT).alarm
    unshelve = pb.UnshelveAlarmRequest(alarm_id=alarm.alarm_id)
    back = alarms.call("UnshelveAlarm", unshelve, OPERATOR, TENANT).alarm
    check(
        shelved.state == pb.ALARM_STATE_SHELVED and back.state == pb.ALARM_STATE_ACKNOWLEDGED,
        "Shelved for 5 minutes, then unshelved, it is acknowledged again",
    )
    with keycloak_public():
        send_to("alarms", token, [report("clear", TAG, datetime.now(UTC).replace(microsecond=0))])
    cleared = wait_for(alarms, TAG, pb.ALARM_STATE_CLEARED)
    check(
        cleared is not None and cleared.acknowledged_by == OPERATOR,
        "The device clears it, and who acknowledged it is kept",
    )
    history = alarms.call("GetAlarm", pb.GetAlarmRequest(alarm_id=alarm.alarm_id), OPERATOR,
                          TENANT).transitions  # fmt: skip
    steps = [(t.to_state, t.actor_type) for t in history]
    check(
        steps == [
            (pb.ALARM_STATE_ACTIVE, "device"), (pb.ALARM_STATE_ACKNOWLEDGED, "user"),
            (pb.ALARM_STATE_SHELVED, "user"), (pb.ALARM_STATE_ACKNOWLEDGED, "user"),
            (pb.ALARM_STATE_CLEARED, "device"),
        ],
        "Its history has every change, each by the device or the user who made it",
    )  # fmt: skip


def check_announced(alarm: Any) -> None:
    published = "0"
    for _ in range(20):
        published = sql(
            "SELECT count(*) FROM outbox_events WHERE subject = 'alarm.changed' "
            "AND published_at IS NOT NULL AND created_at > now() - interval '5 minutes'"
        )
        if int(published) >= 5:
            break
        time.sleep(1)
    check(int(published) >= 5, f"Every change was published as alarm.changed ({published})")


def audited(audit: Caller, action: str, alarm_id: str) -> bool:
    """Whether the trail has this action on this alarm, looking through every page of it."""
    token = ""
    while True:
        request = audit_pb2.ListAuditRecordsRequest(page_size=500, page_token=token, action=action)
        page = audit.call("ListAuditRecords", request, AUDITOR, TENANT)
        if any(record.resource_id == alarm_id for record in page.records):
            return True
        token = page.next_page_token
        if not token:
            return False


def check_audit(bundle: ClientBundle, key: str, alarm: Any) -> None:
    with (
        port_forward("deployment/windops-audit", APPLICATION, 50051) as port,
        blocking_channel(port, AUDIT_HOST, bundle) as channel,
    ):
        audit = Caller(audit_pb2_grpc.AuditServiceStub(channel), key)
        wanted = {"alarm.acknowledged", "alarm.shelved", "alarm.unshelved"}
        found: set[str] = set()
        for _ in range(30):
            found = {action for action in wanted if audited(audit, action, alarm.alarm_id)}
            if found == wanted:
                break
            time.sleep(2)
    check(
        {"alarm.acknowledged", "alarm.shelved", "alarm.unshelved"} <= found,
        "Each of the operator's actions reached the tamper-evident audit trail",
    )


def clean_up() -> None:
    sql(
        "DELETE FROM alarm_transitions WHERE alarm_id IN (SELECT alarm_id FROM alarms "
        "WHERE tag LIKE 'alarm-check-%'); DELETE FROM alarms WHERE tag LIKE 'alarm-check-%'; "
        "DELETE FROM alarm_reports WHERE tag LIKE 'alarm-check-%'; SELECT 1"
    )


def main() -> int:
    try:
        clean_up()
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, OPERATOR, "control-room-operator")
        bootstrap(TENANT, AUDITOR, "auditor")
        bootstrap(OTHER, STRANGER, "control-room-operator")
        token = device_token(TENANT, TURBINE)
        with (
            port_forward("deployment/windops-alarm", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            alarms = Caller(alarm_pb2_grpc.AlarmServiceStub(channel), key)
            alarm = check_reported(alarms, token)
            check_who_may_act(alarms, alarm)
            check_operator(alarms, alarm, token)
        check_announced(alarm)
        check_audit(bundle, key, alarm)
    except (
        RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Alarm check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
