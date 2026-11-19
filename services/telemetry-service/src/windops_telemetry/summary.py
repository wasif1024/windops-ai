"""A frozen snapshot, summarized per metric, for agents that cannot read every point (M9.0).

Worked out in one pass over the frozen samples, in their order, so the same snapshot always gives
the same summary. For each metric:

- **readings:** every point, then good, suspect and missing, by its quality;
- **values:** the minimum, maximum, mean, first and last of the readings that have one. A missing
  reading is counted, never averaged;
- **trend:** the least-squares slope against time, in the metric's unit per hour;
- **buckets:** the window cut into equal slices, each with its readings and their mean.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from windops.telemetry.v1 import telemetry_pb2 as pb

from windops_telemetry.schema import UNITS
from windops_telemetry.snapshots import Sample, quality_of, timestamp, utc

DEFAULT_BUCKETS, MAXIMUM_BUCKETS = 12, 48


@dataclass
class _Metric:
    metric: int
    reading: str
    buckets: int
    counts: dict[int, int] = field(default_factory=dict)
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    first: float | None = None
    last: float | None = None
    first_at: datetime | None = None
    last_at: datetime | None = None
    # Sums for the least-squares slope, with time in hours from the window's start.
    st: float = 0.0
    stt: float = 0.0
    sv: float = 0.0
    stv: float = 0.0
    slices: list[list[float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.slices = [[0.0, 0.0] for _ in range(self.buckets)]


class Summary:
    """Takes a snapshot's samples one at a time, and gives each wanted metric's summary."""

    def __init__(self, start: datetime, end: datetime, wanted: Sequence[tuple[int, str]],
                 buckets: int = DEFAULT_BUCKETS) -> None:  # fmt: skip
        if not 1 <= buckets <= MAXIMUM_BUCKETS:
            raise ValueError(f"buckets is from 1 to {MAXIMUM_BUCKETS}")
        self.start, self.end = utc(start), utc(end)
        self._span = max((self.end - self.start).total_seconds(), 1.0)
        self._metrics = [_Metric(metric, reading, buckets) for metric, reading in wanted]

    def add(self, sample: Sample) -> None:
        at = utc(sample.event_time)
        hours = (at - self.start).total_seconds() / 3600
        where = (at - self.start).total_seconds() / self._span
        for m in self._metrics:
            value = sample.readings.get(m.reading)
            quality = quality_of(sample.label, value)
            m.counts[quality] = m.counts.get(quality, 0) + 1
            if value is None or quality == pb.QUALITY_MISSING:
                continue
            m.count += 1
            m.total += value
            m.minimum = value if m.minimum is None else min(m.minimum, value)
            m.maximum = value if m.maximum is None else max(m.maximum, value)
            if m.first is None:
                m.first, m.first_at = value, at
            m.last, m.last_at = value, at
            m.st, m.stt, m.sv, m.stv = m.st + hours, m.stt + hours * hours, m.sv + value, (
                m.stv + hours * value)  # fmt: skip
            index = min(max(int(where * m.buckets), 0), m.buckets - 1)
            m.slices[index][0] += 1
            m.slices[index][1] += value

    def metrics(self) -> list[pb.MetricSummary]:
        out = []
        for m in self._metrics:
            width = (self.end - self.start) / m.buckets
            summary = pb.MetricSummary(
                metric=m.metric,  # type: ignore[arg-type]
                unit=UNITS[m.reading], readings=sum(m.counts.values()),
                good=m.counts.get(pb.QUALITY_GOOD, 0), suspect=m.counts.get(pb.QUALITY_SUSPECT, 0),
                missing=m.counts.get(pb.QUALITY_MISSING, 0),
                first_at=timestamp(m.first_at), last_at=timestamp(m.last_at),
            )  # fmt: skip
            if m.minimum is not None and m.maximum is not None:
                summary.minimum, summary.maximum = m.minimum, m.maximum
                summary.mean = m.total / m.count
            if m.first is not None and m.last is not None:
                summary.first, summary.last = m.first, m.last
            spread = m.count * m.stt - m.st * m.st
            if m.count >= 2 and spread > 1e-12:
                summary.trend_per_hour = (m.count * m.stv - m.st * m.sv) / spread
            for index, (count, total) in enumerate(m.slices):
                bucket = pb.SummaryBucket(
                    start=timestamp(self.start + width * index),
                    end=timestamp(self.start + width * (index + 1)), readings=int(count),
                )  # fmt: skip
                if count:
                    bucket.mean = total / count
                summary.buckets.append(bucket)
            out.append(summary)
        return out
