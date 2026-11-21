"""Keep one consumer reading until told to stop, through outages (RES-006)."""

import asyncio
import contextlib
import logging

from nats.errors import Error as NatsError
from nats.js.errors import APIError

from windops_events.backend import Consumer
from windops_events.client import EventsUnavailableError

logger = logging.getLogger("windops.events")
RETRY_SECONDS = 5


async def keep_consuming(consumer: Consumer, stop: asyncio.Event, what: str) -> None:
    """Start it, then read batch after batch. A back end that cannot be reached is tried again
    after a pause, never restarting the service. `what` names it in the logs."""
    started = False
    while not stop.is_set():
        try:
            if not started:
                await consumer.start()
                started = True
            await consumer.run_once()
        except (NatsError, APIError, EventsUnavailableError, TimeoutError, OSError) as error:
            logger.warning(f"Cannot read {what} yet", extra={"error_type": type(error).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RETRY_SECONDS)
