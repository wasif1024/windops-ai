"""What a device sends, and how it can go wrong on the way (SIM-004).

The physics make a sample. A delivery is that sample as a device sends it: usually once, a moment
after it is measured. These problems are injected at random, from their own stream, at the rates
asked for, and each one is recorded in the ground truth, so detection can be scored (FR-TEL-003):

- missing: never sent
- duplicated: sent twice
- delayed: sent minutes late
- malformed: sent broken, with a field missing, a value of the wrong type, or text that is not JSON
- out_of_order: sent after the sample that follows it
"""

import heapq
import json
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

KINDS = ("missing", "duplicated", "delayed", "malformed", "out_of_order")
# A device's normal delay from measuring to sending.
NORMAL_LATENCY = timedelta(milliseconds=200)
MALFORMATIONS = ("missing_field", "wrong_type", "not_json")


@dataclass(frozen=True)
class DataProblems:
    """The chance, for each sample, of each problem. Zero for all is a perfect link."""

    missing: float = 0.0
    duplicated: float = 0.0
    delayed: float = 0.0
    malformed: float = 0.0
    out_of_order: float = 0.0
    delay: tuple[timedelta, timedelta] = (timedelta(seconds=30), timedelta(minutes=10))

    def __post_init__(self) -> None:
        rates = [getattr(self, kind) for kind in KINDS]
        if any(not 0 <= rate <= 1 for rate in rates) or sum(rates) > 1:
            raise ValueError("Each rate is from 0 to 1, and together at most 1")


@dataclass(frozen=True, order=True)
class Delivery:
    """One message as a device sends it, ordered by when it is sent."""

    sent_at: datetime
    order: int
    # The message: a sample's fields, or for "not_json" the broken text itself.
    payload: dict[str, Any] | str = field(compare=False)

    def as_line(self) -> str:
        return self.payload if isinstance(self.payload, str) else json.dumps(self.payload)


@dataclass(frozen=True)
class Problem:
    """One injected problem, for the ground truth. Never sent with the data."""

    kind: str
    turbine_id: str
    sequence: int
    event_time: datetime
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "turbine_id": self.turbine_id,
            "sequence": self.sequence,
            "event_time": self.event_time.isoformat(),
            "detail": self.detail,
        }


def _malformed(payload: dict[str, Any], how: str) -> dict[str, Any] | str:
    if how == "missing_field":
        return {name: value for name, value in payload.items() if name != "event_time"}
    if how == "wrong_type":
        return {**payload, "power_kw": f"{payload['power_kw']}kW"}
    # Cut off part way through, as a dropped connection leaves it.
    text = json.dumps(payload)
    return text[: len(text) // 2]


class Link:
    """A device's connection: turns samples into deliveries, with the problems asked for."""

    def __init__(self, problems: DataProblems, rng: random.Random) -> None:
        self.problems = problems
        self.random = rng
        self.injected: list[Problem] = []
        self._order = 0
        self._held: dict[str, tuple[dict[str, Any], datetime, int]] = {}

    def _send(self, payload: dict[str, Any] | str, at: datetime) -> Delivery:
        self._order += 1
        return Delivery(at, self._order, payload)

    def carry(self, payload: dict[str, Any], event_time: datetime) -> list[Delivery]:
        """The deliveries for one sample, which may be none, one or two."""
        turbine, sequence = str(payload["turbine_id"]), int(payload["sequence"])
        out: list[Delivery] = []
        held = self._held.pop(turbine, None)
        roll = self.random.random()
        normal = event_time + NORMAL_LATENCY
        problems = self.problems
        edge = 0.0
        kind = ""
        for name in KINDS:
            edge += getattr(problems, name)
            if roll < edge:
                kind = name
                break
        if kind == "missing":
            self.injected.append(Problem(kind, turbine, sequence, event_time))
        elif kind == "duplicated":
            out += [self._send(payload, normal), self._send(dict(payload), normal + NORMAL_LATENCY)]
            self.injected.append(Problem(kind, turbine, sequence, event_time))
        elif kind == "delayed":
            low, high = problems.delay
            late = low + (high - low) * self.random.random()
            out.append(self._send(payload, event_time + late))
            self.injected.append(
                Problem(kind, turbine, sequence, event_time, f"{int(late.total_seconds())}s")
            )
        elif kind == "malformed":
            how = self.random.choice(MALFORMATIONS)
            out.append(self._send(_malformed(payload, how), normal))
            self.injected.append(Problem(kind, turbine, sequence, event_time, how))
        elif kind == "out_of_order" and held is None:
            # Held back, and sent just after the next sample.
            self._held[turbine] = (payload, event_time, sequence)
            self.injected.append(Problem(kind, turbine, sequence, event_time))
            return out
        else:
            out.append(self._send(payload, normal))
        if held is not None:
            held_payload, _, _ = held
            out.append(self._send(held_payload, normal + NORMAL_LATENCY))
        return out

    def flush(self, at: datetime) -> list[Delivery]:
        """Send what is still held back, when the run ends."""
        held = [self._send(payload, at) for payload, _, _ in self._held.values()]
        self._held.clear()
        return held


def in_send_order(
    batches: Iterable[tuple[datetime, list[Delivery]]],
) -> Iterator[Delivery]:
    """Release deliveries in the order they are sent, from batches in measurement order.

    A later sample is always measured later, and sent no sooner, so anything due to be sent by a
    batch's measurement time can go. Only delayed deliveries wait in memory.
    """
    waiting: list[Delivery] = []
    for measured_at, deliveries in batches:
        for delivery in deliveries:
            heapq.heappush(waiting, delivery)
        while waiting and waiting[0].sent_at <= measured_at:
            yield heapq.heappop(waiting)
    while waiting:
        yield heapq.heappop(waiting)
