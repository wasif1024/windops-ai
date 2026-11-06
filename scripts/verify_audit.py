#!/usr/bin/env python3
"""Prove the Audit Service works on the real local cluster, including real outages.

Run `make secrets`, `make infra` and `make services` first, because every read asks the
Authorization Service for audit.read. This deploys the service, and leaves it running. It stops
PostgreSQL and restarts NATS for a moment. Its test records and events are removed afterwards.
"""

import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import grpc
from gateway_routes import port_forward
from nats.aio.client import Client
from nats.errors import Error as NatsError
from nats_access import ADMIN_USER as NATS_ADMIN
from nats_access import SECRETS_FILE as NATS_SECRETS
from provision_postgres import ADMIN_KEY, CONTEXT, POD, database_name, read_passwords
from provision_postgres import ADMIN_USER as POSTGRES_ADMIN
from provision_postgres import NAMESPACE as DATA
from run_migrations import job_manifests, run_job
from tls_client import async_channel, authority_file, client_bundle
from verify_cluster import minikube_docker_env
from windops.audit.v1 import audit_pb2, audit_pb2_grpc
from windops.events.v1 import events_pb2
from windops_events import DEAD_LETTER_STREAM, audit_request_subject, build_envelope, connect
from windops_grpc import VerifiedIdentity, sign_identity

REPOSITORY = Path(__file__).resolve().parent.parent
APPLICATION = "windops-application"
DEPLOYMENT = "deployment/windops-audit"
IMAGE = "windops-audit:local"
AUDIT_HOST = "windops-audit.windops-application.svc.cluster.local"
DATABASE = database_name("audit")
RUN = uuid.uuid4().hex[:8]
TENANT = f"audit-check-{RUN}"
OTHER_TENANT = f"audit-check-other-{RUN}"
# Made an Auditor in both test tenants, since reading needs audit.read (SRS 3.2).
READER = "audit-check"
AUDIT_STREAM = "WINDOPS_AUDIT"
EVENTS_STREAM = "WINDOPS_EVENTS"
POSTGRES = "statefulset/windops-postgres"
NATS_POD = "windops-nats-0"
SIGNING_KEY_FILE = REPOSITORY / "infrastructure" / "minikube" / "secrets.local.env"
REQUEST = events_pb2.AuditRequested(
    action="alarm.acknowledged",
    actor_type="agent",
    actor_id="fault-investigator",
    on_behalf_of="user-1",
    resource_type="alarm",
    resource_id="a-1",
    outcome="succeeded",
    correlation_id=f"trace-{RUN}",
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str, stdin: str | None = None, required: bool = True) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, *arguments],
        input=stdin, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if required and result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "kubectl", "--context", CONTEXT, f"--namespace={DATA}", "exec", POD, "--", "psql",
            "--no-psqlrc", "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1",
            f"--username={POSTGRES_ADMIN}", f"--dbname={DATABASE}", f"--command={sql}",
        ],
        capture_output=True, text=True, check=False,
    )  # fmt: skip


def grant_reader() -> None:
    for tenant in (TENANT, OTHER_TENANT):
        kubectl(
            "exec", "deployment/windops-authorization", "--namespace=windops-system", "--",
            "python", "-m", "windops_authorization.bootstrap",
            "--tenant", tenant, "--user", READER, "--role", "auditor",
        )  # fmt: skip


async def check_reader_needed(api: "AuditApi") -> None:
    code = grpc.StatusCode.OK
    try:
        await api.records(user="audit-check-no-role")
    except grpc.aio.AioRpcError as error:
        code = error.code()
    check(
        code is grpc.StatusCode.PERMISSION_DENIED,
        "someone without audit.read cannot read the trail (SRS 3.2)",
    )


def deploy() -> None:
    build = subprocess.run(
        ["docker", "build", "-q", "-f", "services/audit-service/Dockerfile", "-t", IMAGE, "."],
        cwd=REPOSITORY, env=minikube_docker_env(), capture_output=True, text=True, check=False,
    )  # fmt: skip
    if build.returncode != 0:
        raise RuntimeError(f"The image did not build:\n{build.stderr[-2000:]}")
    audit_job = next(path for path in job_manifests() if path.parts[-3] == "audit-service")
    run_job(audit_job.read_text())
    check(
        psql("SELECT to_regclass('audit_records') IS NOT NULL").stdout.strip() == "t",
        "the migration Job creates the audit tables",
    )
    kubectl("apply", "--kustomize", str(REPOSITORY / "services" / "audit-service" / "deploy"))
    kubectl("rollout", "restart", DEPLOYMENT, f"--namespace={APPLICATION}")
    kubectl("rollout", "status", DEPLOYMENT, f"--namespace={APPLICATION}", "--timeout=240s")


def restarts() -> int:
    output = kubectl(
        "get", "pods", f"--namespace={APPLICATION}",
        "--selector=app.kubernetes.io/name=windops-audit",
        "--output=jsonpath={.items[*].status.containerStatuses[*].restartCount}",
    )  # fmt: skip
    return sum(int(value) for value in output.split())


class AuditApi:
    def __init__(self, port: int, secret: str) -> None:
        self._channel = async_channel(port, AUDIT_HOST, client_bundle())
        self._stub = audit_pb2_grpc.AuditServiceStub(self._channel)
        self._secret = secret

    def _metadata(self, tenant: str, user: str = READER) -> tuple[tuple[str, str], ...]:
        identity = VerifiedIdentity(subject=user, tenant_id=tenant, user_id=user)
        return (("x-windops-identity", sign_identity(identity, self._secret)),)

    async def records(self, tenant: str = TENANT, user: str = READER) -> list[Any]:
        response = await self._stub.ListAuditRecords(
            audit_pb2.ListAuditRecordsRequest(page_size=200),
            metadata=self._metadata(tenant, user),
            timeout=10,
        )
        return list(response.records)

    async def verify(self) -> Any:
        return await self._stub.VerifyAuditChain(
            audit_pb2.VerifyAuditChainRequest(), metadata=self._metadata(TENANT), timeout=10
        )

    async def wait_for(self, event_id: str, *, attempts: int = 60) -> Any:
        for _ in range(attempts):
            try:
                for record in await self.records():
                    if record.event_id == event_id:
                        return record
            except grpc.aio.AioRpcError:
                pass
            await asyncio.sleep(1)
        raise RuntimeError(f"Failed: no record for {event_id}")

    async def close(self) -> None:
        await self._channel.close()


async def request_record(
    client: Client,
    *,
    event_id: str,
    message_id: str | None = None,
    tenant: str = TENANT,
) -> None:
    envelope = build_envelope(
        audit_request_subject("alarm"),
        tenant_id=tenant,
        payload=REQUEST,
        producer="alarm",
        event_id=event_id,
    )
    await client.jetstream().publish(
        audit_request_subject("alarm"),
        envelope.SerializeToString(),
        headers={"Nats-Msg-Id": message_id or event_id},
    )


async def nats_client(port: int, passwords: dict[str, str], service: str | None) -> Client:
    return await connect(
        url=f"tls://127.0.0.1:{port}",
        ca_file=authority_file(),
        username=database_name(service) if service else NATS_ADMIN,
        password=passwords[service or ADMIN_KEY],
        name="audit-check",
    )


async def check_recording(api: AuditApi, alarm: Client) -> None:
    event_id = f"first-{RUN}"
    await request_record(alarm, event_id=event_id)
    record = await api.wait_for(event_id)
    check(
        (record.action, record.producer, record.outcome)
        == ("alarm.acknowledged", "alarm", "succeeded"),
        "an audit request becomes a record",
    )
    check(
        (record.actor_id, record.on_behalf_of) == ("fault-investigator", "user-1"),
        "a record names the agent and the user behind it (SEC-010)",
    )
    check(record.correlation_id == f"trace-{RUN}", "a record keeps its correlation ID (OBS-008)")
    await request_record(alarm, event_id=f"other-{RUN}", tenant=OTHER_TENANT)
    await asyncio.sleep(2)
    # Only Alarm's: making the reader an Auditor left an Authorization record in each tenant too.
    theirs = [item.event_id for item in await api.records(OTHER_TENANT) if item.producer == "alarm"]
    check(
        theirs == [f"other-{RUN}"]
        and f"other-{RUN}" not in [item.event_id for item in await api.records()],
        "a tenant sees only its own records",
    )


async def check_redelivery(api: AuditApi, alarm: Client) -> None:
    """A new message ID gets past JetStream's check, so only the service can catch the repeat."""
    event_id = f"repeat-{RUN}"
    await request_record(alarm, event_id=event_id)
    await api.wait_for(event_id)
    await request_record(alarm, event_id=event_id, message_id=f"{event_id}-again")
    marker = f"after-repeat-{RUN}"
    await request_record(alarm, event_id=marker)
    await api.wait_for(marker)
    count = [item.event_id for item in await api.records()].count(event_id)
    check(count == 1, "a redelivered audit event creates only one record (Phase 2 exit gate)")


def check_append_only() -> None:
    for statement, name in (
        ("UPDATE audit_records SET outcome = 'denied'", "UPDATE"),
        ("DELETE FROM audit_records", "DELETE"),
        ("TRUNCATE audit_records", "TRUNCATE"),
    ):
        result = psql(statement)
        check(
            result.returncode != 0 and "append-only" in result.stderr,
            f"{name} is refused, even for an administrator (FR-AUD-002)",
        )


async def check_tampering(api: AuditApi) -> None:
    valid = await api.verify()
    check(valid.valid and valid.records_checked > 0, "an untouched chain verifies")
    # Only by turning the triggers off, as someone tampering would have to.
    change = (
        "SET session_replication_role = replica; "  # noqa: S608
        f"UPDATE audit_records SET outcome = 'denied' WHERE event_id = 'first-{RUN}'"
    )
    if psql(change).returncode != 0:
        raise RuntimeError("Could not make the test change")
    tampered = await api.verify()
    check(not tampered.valid, "a changed record is detected (FR-AUD-002)")
    undo = (
        "SET session_replication_role = replica; "  # noqa: S608
        f"UPDATE audit_records SET outcome = 'succeeded' WHERE event_id = 'first-{RUN}'"
    )
    if psql(undo).returncode != 0:
        raise RuntimeError("Could not undo the test change")
    check((await api.verify()).valid, "undoing the change makes the chain valid again")


async def check_forgery(api: AuditApi, alarm: Client, admin: Client) -> None:
    js = admin.jetstream()
    before = (await js.stream_info(AUDIT_STREAM)).state.messages
    envelope = build_envelope(
        audit_request_subject("approval"),
        tenant_id=TENANT,
        payload=REQUEST,
        producer="approval",
        event_id=f"forged-{RUN}",
    )
    stored = True
    try:
        await alarm.jetstream().publish(
            audit_request_subject("approval"), envelope.SerializeToString(), timeout=2
        )
    except (NatsError, TimeoutError):
        stored = False
    after = (await js.stream_info(AUDIT_STREAM)).state.messages
    check(not stored and before == after, "Alarm cannot ask for a record in Approval's name")

    # Using its own subject while claiming to be another service.
    envelope.event_id = f"claimed-{RUN}"
    await alarm.jetstream().publish(
        audit_request_subject("alarm"),
        envelope.SerializeToString(),
        headers={"Nats-Msg-Id": envelope.event_id},
    )
    marker = f"after-claim-{RUN}"
    await request_record(alarm, event_id=marker)
    await api.wait_for(marker)
    ids = [item.event_id for item in await api.records()]
    check(f"claimed-{RUN}" not in ids, "a request claiming another service's name makes no record")
    letters = await js.pull_subscribe("deadletter.audit", stream=DEAD_LETTER_STREAM)
    found = False
    try:
        for message in await letters.fetch(100, timeout=3):
            found = found or f"claimed-{RUN}".encode() in message.data
            await message.ack()
    except TimeoutError:
        pass
    await letters.unsubscribe()
    check(found, "that request goes to the dead-letter stream")


async def check_announcement(admin: Client) -> None:
    js = admin.jetstream()
    reader = await js.pull_subscribe("audit.recorded", stream=EVENTS_STREAM)
    found = 0
    for _ in range(10):
        try:
            for message in await reader.fetch(200, timeout=2):
                found += TENANT.encode() in message.data
                await message.ack()
        except TimeoutError:
            if found:
                break
    await reader.unsubscribe()
    check(found > 0, "a stored record is announced as audit.recorded, through the outbox")


async def check_database_outage(api: AuditApi, alarm: Client) -> None:
    """Longer than every retry, so a dead-letter would have happened by the end."""
    before = restarts()
    event_id = f"during-db-outage-{RUN}"
    kubectl("scale", POSTGRES, "--replicas=0", f"--namespace={DATA}")
    kubectl("wait", f"pod/{POD}", "--for=delete", f"--namespace={DATA}", "--timeout=120s")
    await request_record(alarm, event_id=event_id)
    await asyncio.sleep(35)
    kubectl("scale", POSTGRES, "--replicas=1", f"--namespace={DATA}")
    kubectl("rollout", "status", POSTGRES, f"--namespace={DATA}", "--timeout=180s")
    await api.wait_for(event_id, attempts=120)
    check(True, "a request during a database outage is recorded afterwards (SRS 7.7)")
    check(restarts() == before, "a database outage does not restart the service (RES-006)")


async def check_events_outage(api: AuditApi, port_passwords: dict[str, str]) -> None:
    before = restarts()
    kubectl("delete", "pod", NATS_POD, f"--namespace={DATA}")
    kubectl(
        "rollout", "status", "statefulset/windops-nats", f"--namespace={DATA}", "--timeout=180s"
    )
    kubectl(
        "wait", f"pod/{NATS_POD}", "--for=condition=Ready", f"--namespace={DATA}", "--timeout=180s"
    )
    with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
        alarm = await nats_client(port, port_passwords, "alarm")
        event_id = f"after-nats-restart-{RUN}"
        await request_record(alarm, event_id=event_id)
        await api.wait_for(event_id, attempts=120)
        await alarm.close()
    check(True, "recording continues after NATS restarts")
    check(restarts() == before, "a NATS restart does not restart the service (RES-006)")


def clean_up_database() -> None:
    psql(
        "SET session_replication_role = replica; "  # noqa: S608
        f"DELETE FROM audit_records WHERE tenant_id LIKE 'audit-check-%{RUN}'; "
        f"DELETE FROM outbox_events WHERE tenant_id LIKE 'audit-check-%{RUN}'"
    )


async def clean_up_events(port: int, passwords: dict[str, str]) -> None:
    admin = await nats_client(port, passwords, None)
    js = admin.jetstream()
    try:
        for stream, subject in (
            (AUDIT_STREAM, "audit.requested.*"),
            (EVENTS_STREAM, "audit.recorded"),
            (DEAD_LETTER_STREAM, "deadletter.audit"),
        ):
            reader = await js.pull_subscribe(subject, stream=stream)
            while True:
                try:
                    messages = await reader.fetch(200, timeout=1)
                except TimeoutError:
                    break
                for message in messages:
                    if RUN.encode() in message.data and message.metadata.sequence:
                        await js.delete_msg(stream, message.metadata.sequence.stream)
                    await message.ack()
            await reader.unsubscribe()
    finally:
        await admin.close()


async def run(grpc_port: int, secret: str, passwords: dict[str, str]) -> None:
    api = AuditApi(grpc_port, secret)
    try:
        with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
            alarm = await nats_client(port, passwords, "alarm")
            admin = await nats_client(port, passwords, None)
            await check_reader_needed(api)
            await check_recording(api, alarm)
            await check_redelivery(api, alarm)
            check_append_only()
            await check_tampering(api)
            await check_forgery(api, alarm, admin)
            await check_announcement(admin)
            await check_database_outage(api, alarm)
            await alarm.close()
            await admin.close()
        await check_events_outage(api, passwords)
    finally:
        await api.close()


def main() -> int:
    secret = read_passwords(SIGNING_KEY_FILE)["signing-key"]
    passwords = read_passwords(NATS_SECRETS)
    code = 0
    started = time.monotonic()
    try:
        deploy()
        grant_reader()
        with port_forward(DEPLOYMENT, APPLICATION, 50051) as port:
            asyncio.run(run(port, secret, passwords))
    except (RuntimeError, OSError, NatsError, TimeoutError, grpc.aio.AioRpcError,
            subprocess.CalledProcessError) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        code = 1
    finally:
        kubectl("scale", POSTGRES, "--replicas=1", f"--namespace={DATA}", required=False)
        kubectl(
            "rollout", "status", POSTGRES, f"--namespace={DATA}", "--timeout=180s", required=False
        )
        clean_up_database()
        with port_forward(f"pod/{NATS_POD}", DATA, 4222) as port:
            asyncio.run(clean_up_events(port, passwords))
    if code == 0:
        print(f"Audit check passed in {time.monotonic() - started:.0f} seconds")
    return code


if __name__ == "__main__":
    sys.exit(main())
