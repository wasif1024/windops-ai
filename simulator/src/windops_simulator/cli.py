"""Print simulated samples, to look at them or keep a fixture.

    uv run python -m windops_simulator --seed 1001 --minutes 10 --turbine dogger-east-t01
    uv run python -m windops_simulator --seed 1001 --minutes 60 --every 600 --format table

The same seed, start and turbines always print the same samples (SIM-001).
"""

import argparse
import json
import sys
from datetime import datetime

from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.models import MODEL_VERSION
from windops_simulator.simulator import Simulator

# A fixed default, so a run can be repeated exactly. Pass --start for another moment.
DEFAULT_START = "2026-01-15T00:00:00+00:00"
COLUMNS = (
    ("event_time", 25), ("turbine_id", 17), ("wind_speed_mps", 6), ("rotor_speed_rpm", 6),
    ("power_kw", 8), ("gearbox_oil_temp_c", 6), ("main_bearing_temp_c", 6),
    ("nacelle_vibration_mm_s", 6), ("operating_state", 16),
)  # fmt: skip


def parse(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=1001)
    parser.add_argument("--start", default=DEFAULT_START, help="ISO 8601, with a time zone")
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument(
        "--every", type=float, default=1, help="print a sample this many seconds apart"
    )
    parser.add_argument("--turbine", action="append", help="a turbine ID; repeat for more")
    parser.add_argument("--tenant", help="only this tenant's turbines")
    parser.add_argument("--format", choices=("jsonl", "table"), default="jsonl")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    options = parse(sys.argv[1:] if arguments is None else arguments)
    turbines = [
        spec
        for spec in DEMO_TURBINES
        if (not options.turbine or spec.turbine_id in options.turbine)
        and (not options.tenant or spec.tenant_id == options.tenant)
    ]
    if not turbines:
        print("No such turbine in the demo fleet", file=sys.stderr)
        return 2
    simulator = Simulator(options.seed, turbines, datetime.fromisoformat(options.start))
    if options.format == "table":
        print(f"Synthetic telemetry, models {MODEL_VERSION}, seed {options.seed}")
        print(" ".join(name[:width].ljust(width) for name, width in COLUMNS))
    # Step every second, as the physics needs, and print only every `--every` seconds.
    seconds = int(options.minutes * 60)
    every = max(1, int(options.every))
    for second in range(1, seconds + 1):
        samples = simulator.step(1.0)
        if second % every:
            continue
        for sample in samples:
            if options.format == "jsonl":
                print(json.dumps(sample.as_dict()))
            else:
                values = sample.as_dict()
                print(" ".join(str(values[name])[:width].ljust(width) for name, width in COLUMNS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
