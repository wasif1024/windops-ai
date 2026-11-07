"""Run the Telemetry Service's upkeep once, now, instead of waiting for its schedule.

Run inside the service's pod, with its own database login:

    python -m windops_telemetry.upkeep --partitions --retention --summaries

--today plans partitions as if it were that day, to make future ones early. Each step prints one
line of JSON with what it did. The service runs the same steps itself, on its own schedule.
"""

import argparse
import asyncio
import json
import sys
from datetime import date

from sqlalchemy.ext.asyncio import create_async_engine
from windops_migrations import database_url

from windops_telemetry import partitions
from windops_telemetry.summaries import Summarizer


async def run(arguments: argparse.Namespace) -> list[dict[str, object]]:
    engine = create_async_engine(database_url())
    out: list[dict[str, object]] = []
    try:
        if arguments.partitions:
            today = date.fromisoformat(arguments.today) if arguments.today else None
            out.append({"made": await partitions.ensure(engine, today)})
        if arguments.retention:
            out.append({"dropped": await partitions.expire(engine)})
        if arguments.summaries:
            done = await Summarizer(engine).run_once()
            out.append({"summarized": len(done)})
    finally:
        await engine.dispose()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--partitions", action="store_true")
    parser.add_argument("--retention", action="store_true")
    parser.add_argument("--summaries", action="store_true")
    parser.add_argument("--today", help="plan partitions as if it were this day, YYYY-MM-DD")
    arguments = parser.parse_args()
    for line in asyncio.run(run(arguments)):
        print(json.dumps(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
