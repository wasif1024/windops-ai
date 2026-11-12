#!/usr/bin/env python3
"""Prove reading telemetry and snapshots on the cluster, against the live fleet (FR-TEL-006).

Run the platform and the simulator first, as `make dev` does. A check-only Reliability Engineer
streams a minute of a demo turbine's telemetry, freezes it as a snapshot, and proves on real
PostgreSQL what SRS 8.4 asks:

- the snapshot has a content hash and a manifest of its sources and versions (EVD-001, EVD-002);
- a sample that arrives late for its window, sent through the gateway as a device would, reaches a
  live read but never the snapshot, which still verifies (EVD-004);
- a retention hold keeps its window until it is released (EVD-003);
- another tenant cannot find it, and row-level security hides its rows.

It removes the late sample and its snapshots at the end. Needs local port 9443 free.
"""

import re
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import grpc
import httpx
from google.protobuf.timestamp_pb2 import Timestamp
from windops.telemetry.v1 import telemetry_pb2 as pb
from windops.telemetry.v1 import telemetry_pb2_grpc

from scripts.checks.fleet.asset import Caller, bootstrap, signing_key
from scripts.checks.fleet.telemetry import (
    APPLICATION,
    HOST,
    as_tenant,
    check,
    device_token,
    samples,
    send,
)
from scripts.lib.tls_client import blocking_channel, client_bundle
from scripts.setup.gateway_routes import port_forward
from scripts.setup.identity_seed import keycloak_public

TENANT, OTHER_TENANT, TURBINE = "north-sea", "atlas-wind", "dogger-east-t01"
READER, OTHER_READER = "snapshot-check-reader", "snapshot-check-other"
SOURCE = "snapshot-check"
GEARBOX = pb.METRIC_GEARBOX_TEMPERATURE


def stamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value)
    return result


def a_finished_minute() -> tuple[datetime, datetime]:
    """The minute before last: over, and long since stored."""
    end = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    return end - timedelta(minutes=1), end


def read(telemetry: Caller, start: datetime, end: datetime, *metrics: Any) -> list[Any]:
    request = pb.StreamTurbineTelemetryRequest(
        turbine_id=TURBINE, from_time=stamp(start), to_time=stamp(end), metrics=list(metrics)
    )
    return list(telemetry.call("StreamTurbineTelemetry", request, READER, TENANT))


def check_reading(telemetry: Caller, start: datetime, end: datetime) -> int:
    points = read(telemetry, start, end, GEARBOX)
    times = [(p.event_time.ToDatetime(), p.sequence) for p in points]
    check(
        len(points) >= 50 and times == sorted(times),
        f"A minute of a live turbine streams in event order ({len(points)} gearbox points)",
    )
    check({p.unit for p in points} == {"degC"}, "Each point carries its unit")
    check(
        {p.quality for p in points} <= {pb.QUALITY_GOOD, pb.QUALITY_SUSPECT, pb.QUALITY_MISSING}
        and all(p.quality_label for p in points),
        "and its quality, from its data-quality label",
    )
    every = read(telemetry, start, end)
    check(len(every) == 12 * len(points), "Every metric is there when none is named")
    return len(points)


def check_snapshot(telemetry: Caller, start: datetime, end: datetime) -> Any:
    key = secrets.token_hex(8)
    request = pb.CreateTelemetrySnapshotRequest(
        turbine_id=TURBINE, from_time=stamp(start), to_time=stamp(end), idempotency_key=key
    )
    made = telemetry.call("CreateTelemetrySnapshot", request, READER, TENANT)
    check(
        made.sample_count > 0 and len(made.content_hash) == 64,
        f"A snapshot freezes the minute: {made.sample_count} samples, "
        f"hash {made.content_hash[:12]}…",
    )
    sources = {(s.source, s.model_version) for s in made.manifest.sources}
    check(
        any(source == "windops-simulator" for source, _ in sources)
        and made.manifest.hash_format
        and made.manifest.service_version,
        f"Its manifest names the sources and versions: {sorted(sources)}",
    )
    again = telemetry.call("CreateTelemetrySnapshot", request, READER, TENANT)
    check(again.snapshot_id == made.snapshot_id, "A retry with the same key is the same snapshot")
    return made


def check_frozen(telemetry: Caller, made: Any, start: datetime, end: datetime, live: int) -> None:
    """EVD-004: a sample arriving late for the window reaches live reads, never the snapshot."""
    late = samples(TURBINE, 1, int(time.time() * 1000), SOURCE)[0]
    late["event_time"] = (start + timedelta(seconds=30, milliseconds=500)).isoformat()
    with keycloak_public():
        sent = send(device_token(TENANT, TURBINE), [late])
    check(sent.status_code == 200, "A late sample for the window is sent through the gateway")
    for _ in range(20):
        if len(read(telemetry, start, end, GEARBOX)) == live + 1:
            break
        time.sleep(1)
    check(len(read(telemetry, start, end, GEARBOX)) == live + 1, "A live read now includes it")
    request = pb.GetTelemetrySnapshotRequest(snapshot_id=made.snapshot_id)
    got = telemetry.call("GetTelemetrySnapshot", request, READER, TENANT).snapshot
    check(
        got.verified and got.content_hash == made.content_hash
        and got.sample_count == made.sample_count,
        "The snapshot does not: read again, it hashes the same, so it can be reproduced",
    )  # fmt: skip
    frozen = list(
        telemetry.call(
            "StreamTelemetrySnapshot",
            pb.StreamTelemetrySnapshotRequest(snapshot_id=made.snapshot_id, metrics=[GEARBOX]),
            READER, TENANT,
        )
    )  # fmt: skip
    check(
        len(frozen) == made.sample_count and SOURCE not in str(frozen),
        "Its frozen points are exactly the samples it froze",
    )


def check_hold_and_tenancy(telemetry: Caller, made: Any) -> None:
    check(live_holds(made.snapshot_id) == "1", "A retention hold keeps its window past retention")
    other = telemetry.code(
        "GetTelemetrySnapshot", pb.GetTelemetrySnapshotRequest(snapshot_id=made.snapshot_id),
        OTHER_READER, OTHER_TENANT,
    )  # fmt: skip
    snapshot_id = checked_id(made.snapshot_id)
    hidden = as_tenant(
        OTHER_TENANT,
        f"SELECT count(*) FROM snapshot_samples WHERE snapshot_id = '{snapshot_id}'",  # noqa: S608
    )
    check(
        other is grpc.StatusCode.NOT_FOUND and hidden == "0",
        "Another tenant cannot find it, and row-level security hides its rows",
    )
    release = pb.ReleaseTelemetrySnapshotRequest(
        snapshot_id=made.snapshot_id, idempotency_key=secrets.token_hex(8)
    )
    released = telemetry.call("ReleaseTelemetrySnapshot", release, READER, TENANT).snapshot
    check(
        released.HasField("released_at") and live_holds(made.snapshot_id) == "0",
        "Releasing it lets the hold go, so normal retention applies again",
    )


def checked_id(snapshot_id: str) -> str:
    """A snapshot ID as the service makes them, so it is safe to write into SQL."""
    if not re.fullmatch(r"tsnap_[0-9a-f]{20}", snapshot_id):
        raise RuntimeError(f"Unexpected snapshot ID: {snapshot_id!r}")
    return snapshot_id


def live_holds(snapshot_id: str) -> str:
    """How many unreleased retention holds the snapshot has."""
    query = (
        "SELECT count(*) FROM retention_holds h JOIN telemetry_snapshots s"  # noqa: S608
        " ON s.hold_id = h.hold_id WHERE h.released_at IS NULL"
        f" AND s.snapshot_id = '{checked_id(snapshot_id)}'"
    )
    return as_tenant(TENANT, query)


# The check's own rows: its late sample's source, and its reader's snapshots. Constants, not input.
CLEAN_UP = (
    f"DELETE FROM operational_samples WHERE source = '{SOURCE}'; "  # noqa: S608
    "DELETE FROM snapshot_samples WHERE snapshot_id IN (SELECT snapshot_id"
    f" FROM telemetry_snapshots WHERE created_by = '{READER}'); "
    "DELETE FROM retention_holds WHERE hold_id IN (SELECT hold_id"
    f" FROM telemetry_snapshots WHERE created_by = '{READER}'); "
    f"DELETE FROM telemetry_snapshots WHERE created_by = '{READER}'"
)


def clean_up() -> None:
    as_tenant(TENANT, CLEAN_UP)


def main() -> int:
    try:
        key, bundle = signing_key(), client_bundle()
        bootstrap(TENANT, READER, "reliability-engineer")
        bootstrap(OTHER_TENANT, OTHER_READER, "reliability-engineer")
        start, end = a_finished_minute()
        with (
            port_forward("deployment/windops-telemetry", APPLICATION, 50051) as port,
            blocking_channel(port, HOST, bundle) as channel,
        ):
            telemetry = Caller(telemetry_pb2_grpc.TelemetryServiceStub(channel), key)
            live = check_reading(telemetry, start, end)
            made = check_snapshot(telemetry, start, end)
            check_frozen(telemetry, made, start, end, live)
            check_hold_and_tenancy(telemetry, made)
    except (
        RuntimeError, OSError, grpc.RpcError, httpx.HTTPError, subprocess.CalledProcessError
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Snapshot check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
