"""Data-quality detection: each sample labelled, and each gap recorded, never dropped (FR-TEL-003).

The summarizer calls this for every 10-minute period it summarizes, so labels follow late data
the same way summaries do. Each sample gets one label, the first that applies:

- `duplicate`: a later copy of a sequence this turbine has already sent. The first stays.
- `delayed`: it arrived more than the delay threshold after it was measured.
- `out_of_order`: a sample with a higher sequence, from the same turbine, arrived before it.
- `ok`: none of these.

`received` means not looked at yet. A missing sample cannot be labelled, as it is not there, so
each run of missing sequences is recorded as a gap. A sequence the service refused as malformed
is not missing: it came, broken, and its rejection says so. A late sample that fills a gap removes
it, as the period is checked again. Malformed samples are the rejections the ingest route keeps.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

OK, DUPLICATE, DELAYED, OUT_OF_ORDER = "ok", "duplicate", "delayed", "out_of_order"
LABELS = (OK, DUPLICATE, DELAYED, OUT_OF_ORDER)
# A device sends within a few seconds of batching, so anything this late was held back somewhere.
DELAY = timedelta(seconds=20)

# One stored row: (sequence, ingested_at, arrival_index), which together say which copy it is.
RowKey = tuple[int, datetime, int]


@dataclass(frozen=True)
class Gap:
    first: int
    last: int

    @property
    def missing(self) -> int:
        return self.last - self.first + 1


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def key_of(row: Any) -> RowKey:
    return (int(row.sequence), _utc(row.ingested_at), int(row.arrival_index or 0))


def label(
    rows: Iterable[Any], context: Iterable[Any] = (), delay: timedelta = DELAY
) -> dict[RowKey, str]:
    """A label for each of `rows`. `context` is the turbine's neighbouring samples, which can
    have arrived first, as the order a sample arrived in reaches past its period."""
    mine = {key_of(row): row for row in rows}
    everything = {**{key_of(row): row for row in context}, **mine}
    arrived = sorted(everything.values(), key=lambda r: (_utc(r.ingested_at), r.arrival_index or 0))
    seen: set[int] = set()
    highest = None
    labels: dict[RowKey, str] = {}
    for row in arrived:
        key, sequence = key_of(row), int(row.sequence)
        if sequence in seen:
            result = DUPLICATE
        elif _utc(row.ingested_at) - _utc(row.event_time) > delay:
            result = DELAYED
        elif highest is not None and sequence < highest:
            result = OUT_OF_ORDER
        else:
            result = OK
        seen.add(sequence)
        # A delayed sample does not make the ones after it look out of order: it is the late one.
        if result in (OK, OUT_OF_ORDER):
            highest = sequence if highest is None else max(highest, sequence)
        if key in mine:
            labels[key] = result
    return labels


def gaps(sequences: Iterable[int], before: int | None, malformed: Iterable[int] = ()) -> list[Gap]:
    """The runs of sequences missing from a period, given the turbine's last one before it."""
    present = sorted(set(sequences) | set(malformed))
    if not present:
        return []
    found = []
    previous = before
    for sequence in present:
        if previous is not None and sequence > previous + 1:
            found.append(Gap(previous + 1, sequence - 1))
        previous = sequence
    return found


def count(labels: Sequence[str] | Iterable[str]) -> dict[str, int]:
    totals = dict.fromkeys(LABELS, 0)
    for value in labels:
        totals[value] += 1
    return totals
