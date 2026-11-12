"""Reading telemetry, and freezing it as snapshots an investigation can reproduce (FR-TEL-006).

SRS 8.4 sets what a snapshot keeps:

- **Frozen data (EVD-004).** A snapshot records which stored samples it holds, by sequence, time of
  arrival and place in their batch, with each one's data-quality label at that moment. A sample is
  never changed once stored, so these keys reproduce exactly what was read, however much data
  arrives late for the window or however its labels change.
- **A content hash (EVD-001).** sha256 over the frozen samples, written in `HASH_FORMAT`.
- **A manifest (EVD-002).** The samples' sources, schema and model versions, the quality counts,
  gaps and rejections in the window, the time it was read and the service release.
- **Kept past retention (EVD-003).** The service holds the window's data until the snapshot is
  released; the partition upkeep keeps a partition a live hold overlaps.

Here are the pure parts, which need no database: the metrics, quality, the hash and the manifest.
Samples always come in ORDER: by event time and sequence, then by arrival.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from windops.telemetry.v1 import telemetry_pb2 as pb

from windops_telemetry.quality import DUPLICATE, OK
from windops_telemetry.schema import UNITS

# Each metric and the reading it is, in the order every point is written.
METRICS: dict[int, str] = {
    pb.METRIC_WIND_SPEED: "wind_speed_mps",
    pb.METRIC_WIND_DIRECTION: "wind_direction_deg",
    pb.METRIC_ROTOR_SPEED: "rotor_speed_rpm",
    pb.METRIC_GENERATOR_SPEED: "generator_speed_rpm",
    pb.METRIC_POWER: "power_kw",
    pb.METRIC_PITCH_ANGLE: "pitch_angle_deg",
    pb.METRIC_AMBIENT_TEMPERATURE: "ambient_temp_c",
    pb.METRIC_GEARBOX_TEMPERATURE: "gearbox_oil_temp_c",
    pb.METRIC_BEARING_TEMPERATURE: "gearbox_bearing_temp_c",
    pb.METRIC_MAIN_BEARING_TEMPERATURE: "main_bearing_temp_c",
    pb.METRIC_GENERATOR_BEARING_TEMPERATURE: "generator_bearing_temp_c",
    pb.METRIC_VIBRATION_RMS: "nacelle_vibration_mm_s",
}
READING_ORDER = tuple(METRICS.values())
# One request reads at most a day: 86,400 samples at 1 Hz, streamed (GRP-008).
MAXIMUM_WINDOW = timedelta(hours=24)
HASH_FORMAT = "windops-telemetry-snapshot-v1"
SNAPSHOT_TABLE = "operational_samples"


class WindowError(ValueError):
    """The request's window or metrics cannot be read."""


@dataclass(frozen=True)
class Sample:
    """One stored sample as a snapshot freezes it: its keys, its label, and its readings."""

    sequence: int
    event_time: datetime
    ingested_at: datetime
    arrival_index: int
    label: str
    source: str
    schema_version: str
    model_version: str
    synthetic: bool
    readings: dict[str, float | None]


def utc(value: datetime) -> datetime:
    """SQLite gives times back without a zone. Every stored time is UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(utc(value))
    return stamp


def window(
    from_time: Timestamp, to_time: Timestamp, now: datetime | None = None, *, past: bool = False
) -> tuple[datetime, datetime]:
    """The request's window, checked. `past` requires it to be over, as a snapshot freezes it."""
    if not from_time.seconds and not from_time.nanos:
        raise WindowError("from_time is required")
    if not to_time.seconds and not to_time.nanos:
        raise WindowError("to_time is required")
    start = from_time.ToDatetime(tzinfo=UTC)
    end = to_time.ToDatetime(tzinfo=UTC)
    if end <= start:
        raise WindowError("to_time must be after from_time")
    if end - start > MAXIMUM_WINDOW:
        raise WindowError("A window is at most 24 hours")
    if past and end > (now or datetime.now(UTC)):
        raise WindowError("A snapshot's window must be over: to_time is in the future")
    return start, end


def readings_for(metrics: Sequence[int]) -> list[tuple[int, str]]:
    """The metrics asked for, in the fixed order, or every one when none is named."""
    if not metrics:
        return list(METRICS.items())
    unknown = [metric for metric in metrics if metric not in METRICS]
    if unknown:
        raise WindowError("Unknown metric")
    wanted = set(metrics)
    return [(metric, reading) for metric, reading in METRICS.items() if metric in wanted]


def first_copies(samples: Iterable[Sample]) -> Iterator[Sample]:
    """Each sequence once, as its first copy: a duplicate is left out (FR-TEL-003).

    `samples` must come in ORDER: by event time and sequence, then arrival, so a sequence's first
    copy comes before any other.
    """
    seen: set[int] = set()
    for sample in samples:
        if sample.label == DUPLICATE or sample.sequence in seen:
            continue
        seen.add(sample.sequence)
        yield sample


def quality_of(label: str, value: float | None) -> int:
    """MISSING when the sensor sent nothing, GOOD for an ok sample, SUSPECT otherwise."""
    if value is None:
        return int(pb.QUALITY_MISSING)
    return int(pb.QUALITY_GOOD if label == OK else pb.QUALITY_SUSPECT)


def points(
    samples: Iterable[Sample], wanted: Sequence[tuple[int, str]]
) -> Iterator[pb.TelemetryPoint]:
    """Each sample's points, one a metric, with its unit and quality."""
    for sample in samples:
        at = timestamp(sample.event_time)
        for metric, reading in wanted:
            value = sample.readings.get(reading)
            yield pb.TelemetryPoint(
                event_time=at,
                metric=metric,  # type: ignore[arg-type]
                value=0.0 if value is None else value,
                unit=UNITS[reading],
                quality=quality_of(sample.label, value),  # type: ignore[arg-type]
                sequence=sample.sequence,
                quality_label=sample.label,
            )


def _number(value: float | None) -> str:
    # repr is the shortest text that reads back as the same float, so it never varies.
    return "null" if value is None else repr(float(value))


class Freezer:
    """Takes a window's samples in ORDER, one at a time, and keeps what a snapshot needs.

    The content hash is sha256 over HASH_FORMAT, which anyone can write again to check it: a header
    line naming the format, tenant, turbine and window, then one line a sample, in event order, of
    its time, sequence, label and every reading in READING_ORDER, "|"-separated.
    """

    def __init__(self, tenant_id: str, turbine_id: str, start: datetime, end: datetime) -> None:
        self._digest = hashlib.sha256()
        header = [HASH_FORMAT, tenant_id, turbine_id, utc(start).isoformat(), utc(end).isoformat()]
        self._digest.update(("|".join(header) + "\n").encode())
        self.keys: list[tuple[int, datetime, int, str]] = []
        self.sources: Counter[tuple[str, str, str, bool]] = Counter()
        self.labels: Counter[str] = Counter()

    def add(self, sample: Sample) -> None:
        fields = [utc(sample.event_time).isoformat(), str(sample.sequence), sample.label]
        fields += [_number(sample.readings.get(reading)) for reading in READING_ORDER]
        self._digest.update(("|".join(fields) + "\n").encode())
        self.keys.append((sample.sequence, sample.ingested_at, sample.arrival_index, sample.label))
        key = (sample.source, sample.schema_version, sample.model_version, sample.synthetic)
        self.sources[key] += 1
        self.labels[sample.label] += 1

    @property
    def content_hash(self) -> str:
        return self._digest.copy().hexdigest()

    def manifest(
        self, *, gaps: Iterable[Any], rejected: int, ingested_before: datetime, service_version: str
    ) -> pb.SnapshotManifest:
        """Where the frozen samples came from, their quality, and what was missing (EVD-002)."""
        sequences = [key[0] for key in self.keys]
        return pb.SnapshotManifest(
            ingested_before=timestamp(ingested_before),
            service_version=service_version,
            hash_format=HASH_FORMAT,
            sources=[
                pb.SnapshotSource(
                    source=source,
                    schema_version=schema,
                    model_version=model,
                    synthetic=synthetic,
                    samples=count,
                )
                for (source, schema, model, synthetic), count in sorted(self.sources.items())
            ],
            quality=[
                pb.QualityCount(label=label, samples=count)
                for label, count in sorted(self.labels.items())
            ],
            gaps=[
                pb.SnapshotGap(
                    first_sequence=gap.first_sequence,
                    last_sequence=gap.last_sequence,
                    missing=gap.missing,
                )
                for gap in sorted(gaps, key=lambda g: g.first_sequence)
            ],
            rejected=rejected,
            first_sequence=min(sequences, default=0),
            last_sequence=max(sequences, default=0),
        )


def content_hash(
    tenant_id: str, turbine_id: str, start: datetime, end: datetime, samples: Iterable[Sample]
) -> str:
    """The content hash of samples in ORDER, as Freezer makes it."""
    freezer = Freezer(tenant_id, turbine_id, start, end)
    for sample in samples:
        freezer.add(sample)
    return freezer.content_hash
