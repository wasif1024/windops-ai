#!/usr/bin/env python3
"""Prove telemetry storage on the cluster: partitions, live summaries, late data and retention.

Run `make migrate`, `make services`, `make telemetry-devices` and leave the simulator streaming
for at least 12 minutes first, so a 10-minute period has closed. It writes its own rows as the
tenant `telemetry-check`, with source `telemetry-storage-check`, and removes them at the end.
Upkeep runs on demand inside the service's pod, rather than waiting for its schedule.
"""

import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.simulator import SOURCE as SIMULATOR

from scripts.checks.fleet.telemetry import kubectl, sql

TENANT, TURBINE, SOURCE = "telemetry-check", "storage-check-t01", "telemetry-storage-check"
DEPLOYMENT = "deployment/windops-telemetry"
CHECK_ROWS = f"tenant_id = '{TENANT}' AND source = '{SOURCE}'"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def as_tenant(tenant: str, statement: str) -> str:
    return sql(f"SELECT set_config('windops.tenant_id', '{tenant}', false); {statement}")


def everyone(statement: str) -> str:
    return sql(f"SELECT set_config('windops.all_tenants', 'on', false); {statement}")


def dropped_by_retention() -> list[str]:
    for line in upkeep("--retention"):
        found = line.get("dropped")
        if isinstance(found, list):
            return [str(name) for name in found]
    raise RuntimeError("Upkeep did not say what it dropped")


def upkeep(*steps: str) -> list[dict[str, object]]:
    done = kubectl(
        "exec", DEPLOYMENT, "--", "python", "-m", "windops_telemetry.upkeep", *steps
    )  # fmt: skip
    if done.returncode != 0:
        raise RuntimeError(f"Upkeep failed: {done.stderr.strip()}")
    return [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]


def wait_until(description: str, condition: Callable[[], bool], seconds: int = 150) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(5)
    raise RuntimeError(f"Timed out waiting until {description}")


def day(moment: datetime) -> str:
    return f"{moment:%Y%m%d}"


def check_partitions() -> None:
    now = datetime.now(UTC)
    days = [f"operational_samples_{day(now + timedelta(days=n))}" for n in range(-1, 4)]
    first = now.replace(day=1)
    months = [f"scada_summaries_{first:%Y%m}", "scada_summaries_default"]
    names = ", ".join(f"'{name}'" for name in [*days, *months, "operational_samples_default"])
    forced = sql(
        f"SELECT count(*) FROM pg_class WHERE relforcerowsecurity AND relname IN ({names})"  # noqa: S608
    )
    check(
        forced == str(len(days) + 3),
        "Daily sample and monthly summary partitions are ready, each with forced "
        "row-level security",
    )


def check_live_summaries() -> None:
    """The simulator's own data: every demo turbine has its last closed period summarized."""
    now = datetime.now(UTC)
    last = now - timedelta(minutes=10, seconds=75)
    period = last.replace(minute=last.minute - last.minute % 10, second=0, microsecond=0)
    expected = len(DEMO_TURBINES)

    def summarized() -> bool:
        found = everyone(
            "SELECT count(*) FROM scada_summaries WHERE period_start = "  # noqa: S608
            f"'{period.isoformat()}' AND source = '{SIMULATOR}'"
        )
        return found == str(expected)

    wait_until(f"all {expected} turbines have a summary for {period:%H:%M}", summarized)
    stats = everyone(
        "SELECT min(sample_count), bool_and(synthetic), bool_and(power_kw_mean IS NOT NULL), "  # noqa: S608
        "bool_and(wind_direction_deg_mean BETWEEN 0 AND 360), count(DISTINCT quality_status) "
        f"FROM scada_summaries WHERE period_start = '{period.isoformat()}' "
        f"AND source = '{SIMULATOR}'"
    ).split("|")
    check(
        int(stats[0]) >= 540 and stats[1:4] == ["t", "t", "t"],
        f"Each demo turbine's {period:%H:%M} summary has its 10 minutes of samples "
        f"(at least {stats[0]}, after light link trouble), synthetic, with every statistic",
    )


def insert_samples(event_times: list[datetime], arrived: datetime | None = None) -> None:
    """Samples as if just stored. Arriving now is the database's own time, as the summarizer
    reads by it: a time taken here, before the insert, could fall behind its watermark."""
    at = "now()" if arrived is None else f"'{arrived.isoformat()}'"
    values = ", ".join(
        f"('{TENANT}', '{TURBINE}', '{moment.isoformat()}', {int(moment.timestamp())}, "
        f"'dev_check', {at}, '{SOURCE}', '2', 'check', true, 'producing', "
        f"true, 'received', 1000)"
        for moment in event_times
    )
    as_tenant(
        TENANT,
        "INSERT INTO operational_samples (tenant_id, turbine_id, event_time, sequence, device_id, "  # noqa: S608
        "ingested_at, source, schema_version, model_version, synthetic, operating_state, "
        f"available, quality_status, power_kw) VALUES {values}; SELECT 1",
    )


def check_late_data() -> None:
    """SRS 8.3: a summary is recalculated when late data arrives for its period."""
    now = datetime.now(UTC)
    start = (now - timedelta(minutes=40)).replace(second=0, microsecond=0)
    start = start.replace(minute=start.minute - start.minute % 10)
    seconds = [start + timedelta(seconds=n) for n in range(600)]
    insert_samples(seconds[:300])
    upkeep("--summaries")
    summary = f"FROM scada_summaries WHERE {CHECK_ROWS} AND period_start = '{start.isoformat()}'"
    first = as_tenant(TENANT, f"SELECT revision, sample_count, late_samples {summary}")
    check(first == "1|300|0", "Half a period summarizes as revision 1, with its 300 samples")
    insert_samples(seconds[300:])
    # The service summarizes every minute too, and a run skips while another holds the lock.
    again = ""
    for _ in range(8):
        upkeep("--summaries")
        again = as_tenant(
            TENANT,
            f"SELECT revision, sample_count, late_samples, quality_status {summary}",
        )
        if again == "2|600|300|complete":
            break
        time.sleep(10)
    check(
        again == "2|600|300|complete",
        "Late samples recalculate it as revision 2, complete, counting the 300 late ones",
    )


def check_default_partition_move() -> None:
    """A row already in the default partition moves in when its partition is made."""
    ahead = datetime.now(UTC) + timedelta(days=5)
    moment = ahead.replace(hour=12, minute=0, second=0, microsecond=0)
    insert_samples([moment], datetime.now(UTC))
    where = f"{CHECK_ROWS} AND event_time = '{moment.isoformat()}'"
    held = as_tenant(TENANT, f"SELECT tableoid::regclass FROM operational_samples WHERE {where}")  # noqa: S608
    check(
        held == "operational_samples_default", "A sample with no partition yet waits in the default"
    )
    upkeep("--partitions", "--today", (datetime.now(UTC) + timedelta(days=3)).date().isoformat())
    moved = as_tenant(TENANT, f"SELECT tableoid::regclass FROM operational_samples WHERE {where}")  # noqa: S608
    check(
        moved == f"operational_samples_{day(moment)}",
        "Making its partition moves it there, instead of failing (SRS 8.3)",
    )


def check_retention() -> None:
    """Whole partitions past retention are dropped, unless a snapshot's hold overlaps them."""
    now = datetime.now(UTC)
    old = (now - timedelta(days=200)).date()
    ancient = (now - timedelta(days=800)).date().replace(day=1)
    following = (ancient.replace(day=28) + timedelta(days=4)).replace(day=1)
    raw_name, summary_name = f"operational_samples_{old:%Y%m%d}", f"scada_summaries_{ancient:%Y%m}"
    sql(
        f"CREATE TABLE IF NOT EXISTS {raw_name} PARTITION OF operational_samples "
        f"FOR VALUES FROM ('{old}') TO ('{old + timedelta(days=1)}'); "
        f"CREATE TABLE IF NOT EXISTS {summary_name} PARTITION OF scada_summaries "
        f"FOR VALUES FROM ('{ancient}') TO ('{following}'); SELECT 1"
    )
    hold = f"check-{uuid.uuid4().hex[:12]}"
    as_tenant(
        TENANT,
        "INSERT INTO retention_holds (tenant_id, hold_id, table_name, starts_at, ends_at, "  # noqa: S608
        "reason, "
        f"created_at) VALUES ('{TENANT}', '{hold}', 'operational_samples', '{old}T06:00:00Z', "
        f"'{old}T07:00:00Z', 'telemetry-storage-check', now()); SELECT 1",
    )
    dropped = dropped_by_retention()
    check(
        summary_name in dropped and raw_name not in dropped,
        "A summary partition past 2 years is dropped whole; a held sample partition is kept",
    )
    as_tenant(
        TENANT,
        f"UPDATE retention_holds SET released_at = now() WHERE hold_id = '{hold}'; SELECT 1",  # noqa: S608
    )
    check(
        raw_name in dropped_by_retention(),
        "Once the hold is released, the old sample partition is dropped",
    )


def clean_up() -> None:
    as_tenant(
        TENANT,
        f"DELETE FROM operational_samples WHERE {CHECK_ROWS}; "  # noqa: S608
        f"DELETE FROM scada_summaries WHERE {CHECK_ROWS}; "
        f"DELETE FROM retention_holds WHERE tenant_id = '{TENANT}'; SELECT 1",
    )
    # The partitions it made early, beyond the usual 3 days ahead, now empty. The service makes
    # them again in time, and the next run starts from the same place.
    usual = f"operational_samples_{day(datetime.now(UTC) + timedelta(days=3))}"
    early = sql(
        "SELECT string_agg(child.relname, ',') FROM pg_inherits "  # noqa: S608
        "JOIN pg_class parent ON parent.oid = pg_inherits.inhparent "
        "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
        "WHERE parent.relname = 'operational_samples' AND child.relname ~ '_[0-9]{8}$' "
        f"AND child.relname > '{usual}'"
    )
    for name in filter(None, early.split(",")):
        sql(f"DROP TABLE {name}; SELECT 1")


def main() -> int:
    try:
        check_partitions()
        check_live_summaries()
        check_late_data()
        check_default_partition_move()
        check_retention()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        clean_up()
    print("Telemetry storage check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
