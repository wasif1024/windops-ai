"""Turn each alarm.reported into alarm state, once each, whatever order reports come in."""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, RetryLaterError

from windops_alarm.lifecycle import apply_report, expire_shelves

logger = logging.getLogger("windops.service")
SUBJECT = "alarm.reported"
# Only the Telemetry Service's device route may say what a device reported.
PRODUCER = "telemetry"
SHELF_SECONDS = 30


async def handle(engine: AsyncEngine, event: Event) -> bool:
    """Apply one report. False for a repeat. Safe to run again for the same event (EVT-003)."""
    if event.producer != PRODUCER or not isinstance(event.payload, events_pb2.AlarmReported):
        raise InvalidEventError(f"alarm.reported must come from {PRODUCER}")
    report = event.payload
    if report.action not in ("raise", "clear") or not report.turbine_id or not report.code:
        raise InvalidEventError("An alarm report needs a turbine, a code and an action")
    try:
        async with engine.begin() as connection:
            return await apply_report(
                connection, event.tenant_id, event.event_id, report, datetime.now(UTC)
            )
    except IntegrityError as error:
        # Another replica applied a report for the same condition first: try again after it.
        raise RetryLaterError("A concurrent change won") from error
    except OperationalError as error:
        # The report is fine, so it waits out the outage (SRS 7.7).
        raise RetryLaterError(type(error).__name__) from error


async def keep_expiring(engine: AsyncEngine, stop: asyncio.Event) -> None:
    """Put shelved alarms back when their time is up."""
    while not stop.is_set():
        try:
            done = await expire_shelves(engine)
            if done:
                logger.info("Shelved alarms returned", extra={"alarms": done})
        except Exception as error:  # Tried again shortly. Nothing is lost meanwhile.
            logger.warning(
                "Could not return shelved alarms", extra={"error_type": type(error).__name__}
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SHELF_SECONDS)
