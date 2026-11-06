"""Scenarios: a seed, a fleet, faults and data problems, with hidden ground truth (SIM-005).

Running one gives two things, kept apart. The deliveries are what devices send, and carry no hint
of a fault. The ground truth says what really happened: which fault, where, from when, and every
injected data problem. It goes to the evaluation system, never to the agents, so it is written
to its own file and never mixed into the stream.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from windops_simulator import models
from windops_simulator.delivery import DataProblems, Delivery, Link, Problem, in_send_order
from windops_simulator.faults import FaultSpec
from windops_simulator.fleet import DEMO_TURBINES, TurbineSpec
from windops_simulator.simulator import Simulator, random_stream

GROUND_TRUTH_VERSION = "1"


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    description: str
    seed: int
    start: datetime
    duration: timedelta
    turbines: tuple[TurbineSpec, ...]
    faults: tuple[FaultSpec, ...] = ()
    data_problems: DataProblems = field(default_factory=DataProblems)
    # When a good detector could first see it, for evaluation: the onset, or later for slow faults.
    detectable_after: timedelta | None = None


@dataclass
class GroundTruth:
    """What really happened. For the evaluation system only."""

    scenario: Scenario
    problems: list[Problem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        s = self.scenario
        counts: dict[str, int] = {}
        for problem in self.problems:
            counts[problem.kind] = counts.get(problem.kind, 0) + 1
        return {
            "ground_truth_version": GROUND_TRUTH_VERSION,
            "scenario_id": s.scenario_id,
            "description": s.description,
            "seed": s.seed,
            "model_version": models.MODEL_VERSION,
            "start": s.start.isoformat(),
            "duration_seconds": int(s.duration.total_seconds()),
            "turbines": [spec.turbine_id for spec in s.turbines],
            "faults": [
                {
                    "label": fault.label,
                    "profile": fault.profile,
                    "turbine_id": fault.turbine_id,
                    "component": fault.affected_component,
                    "channel": fault.channel if fault.profile.startswith("sensor_") else "",
                    "severity": fault.severity,
                    "onset": (s.start + fault.onset_after).isoformat(),
                    "full_severity_at": (s.start + fault.onset_after + fault.ramp).isoformat(),
                    "detectable_after": (
                        s.start + (s.detectable_after or fault.onset_after)
                    ).isoformat(),
                }
                for fault in s.faults
            ],
            # A scenario with no fault, or only the false positive, expects "no_fault".
            "expected_label": next(
                (fault.label for fault in s.faults if fault.label != "no_fault"), "no_fault"
            ),
            "data_problems": {"counts": counts, "items": [p.as_dict() for p in self.problems]},
            "synthetic": True,
        }

    def write(self, path: str) -> None:
        with open(path, "w") as file:
            json.dump(self.as_dict(), file, indent=2)
            file.write("\n")


def run(scenario: Scenario, interval: float = 1.0) -> tuple[Iterator[Delivery], GroundTruth]:
    """The deliveries, in the order they are sent, and the ground truth.

    The ground truth is complete once every delivery has been read, as problems are recorded as
    they are injected.
    """
    truth = GroundTruth(scenario)
    simulator = Simulator(scenario.seed, scenario.turbines, scenario.start, scenario.faults)
    links = {
        spec.turbine_id: Link(
            scenario.data_problems,
            random_stream(scenario.seed, f"link:{spec.tenant_id}:{spec.turbine_id}"),
        )
        for spec in scenario.turbines
    }

    def batches() -> Iterator[tuple[datetime, list[Delivery]]]:
        steps = int(scenario.duration.total_seconds() / interval)
        for _ in range(steps):
            out: list[Delivery] = []
            for sample in simulator.step(interval):
                out += links[sample.turbine_id].carry(sample.as_dict(), sample.event_time)
            yield simulator.now, out
        end = [d for link in links.values() for d in link.flush(simulator.now)]
        for link in links.values():
            truth.problems += link.injected
        yield simulator.now, end

    return in_send_order(batches()), truth


def _one(turbine_id: str) -> tuple[TurbineSpec, ...]:
    return tuple(spec for spec in DEMO_TURBINES if spec.turbine_id == turbine_id)


START = datetime.fromisoformat("2026-01-15T06:00:00+00:00")
HOURS = timedelta(hours=1)
# Light, realistic link trouble in every fault scenario, so detection is never on clean data.
LIGHT = DataProblems(
    missing=0.002, duplicated=0.001, delayed=0.001, malformed=0.0005, out_of_order=0.001
)

SCENARIOS = {
    "gearbox-temp-001": Scenario(
        "gearbox-temp-001",
        "Gearbox temperature rises with abnormal vibration: failing oil cooling",
        1001, START, 6 * HOURS, _one("dogger-east-t01"),
        (FaultSpec("gearbox_oil_overheating", "dogger-east-t01", 1 * HOURS, 3 * HOURS),),
        LIGHT, detectable_after=2 * HOURS,
    ),
    "bearing-wear-001": Scenario(
        "bearing-wear-001",
        "The main bearing wears: rising temperature and broadband vibration, accelerating",
        1002, START, 8 * HOURS, _one("hornsea-west-t02"),
        (
            FaultSpec(
                "bearing_wear", "hornsea-west-t02", 1 * HOURS, 6 * HOURS, component="main_bearing"
            ),
        ),
        LIGHT, detectable_after=4 * HOURS,
    ),
    "imbalance-001": Scenario(
        "imbalance-001",
        "Rotor imbalance: vibration growing with the square of rotor speed, temperatures normal",
        1003, START, 6 * HOURS, _one("atlas-ridge-t03"),
        (FaultSpec("imbalance", "atlas-ridge-t03", 1 * HOURS, 2 * HOURS),),
        LIGHT, detectable_after=2 * HOURS,
    ),
    "sensor-drift-001": Scenario(
        "sensor-drift-001",
        "The gearbox oil sensor drifts upward while the gearbox itself is healthy",
        1004, START, 6 * HOURS, _one("dogger-east-t02"),
        (FaultSpec("sensor_drift", "dogger-east-t02", 1 * HOURS, 1 * HOURS),),
        LIGHT, detectable_after=3 * HOURS,
    ),
    "sensor-dropout-001": Scenario(
        "sensor-dropout-001",
        "The main bearing temperature sensor drops out in stretches",
        1005, START, 4 * HOURS, _one("hornsea-west-t03"),
        (
            FaultSpec(
                "sensor_dropout", "hornsea-west-t03", 1 * HOURS, 1 * HOURS,
                channel="main_bearing_temp_c",
            ),
        ),
        LIGHT, detectable_after=1 * HOURS,
    ),
    "high-load-001": Scenario(
        "high-load-001",
        "A false positive: sustained strong wind runs a healthy turbine hot, and it is no fault",
        1006, START, 6 * HOURS, _one("dogger-east-t03"),
        (FaultSpec("false_positive_high_load", "dogger-east-t03", 1 * HOURS, 1 * HOURS),),
        LIGHT,
    ),
    "data-quality-001": Scenario(
        "data-quality-001",
        "A healthy north-sea fleet over a poor link: every kind of data problem, no fault",
        1007, START, 1 * HOURS,
        tuple(spec for spec in DEMO_TURBINES if spec.tenant_id == "north-sea"),
        (),
        DataProblems(
            missing=0.01, duplicated=0.01, delayed=0.005, malformed=0.005, out_of_order=0.01
        ),
    ),
}  # fmt: skip
