"""Turn each fault.detected into a case, or into a detection of a case already open."""

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from windops.events.v1 import events_pb2
from windops_events import Event, InvalidEventError, RetryLaterError

from windops_investigation import investigations
from windops_investigation.cases import attach_detection

SUBJECT = "fault.detected"
# Only the Fault Detection Service may say a fault was detected.
PRODUCER = "fault-detection"
ACTOR = f"service:{PRODUCER}"


async def handle(engine: AsyncEngine, event: Event, investigate: bool = False) -> bool:
    """Attach one detection. False for a repeat, or a low one with nothing to join (EVT-003).
    With investigate, a case it opens is investigated once its evidence is frozen (M8.11)."""
    if event.producer != PRODUCER or not isinstance(event.payload, events_pb2.FaultDetected):
        raise InvalidEventError(f"fault.detected must come from {PRODUCER}")
    detected = event.payload
    if not detected.detection_id or not detected.turbine_id or not detected.rule_id:
        raise InvalidEventError("A detection needs its ID, turbine and rule")
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            case = await attach_detection(connection, event.tenant_id, detected, ACTOR, now)
            if investigate and case is not None and case.version == 1:
                # A new case: investigated as the tenant's fault-investigator (ADR 0019).
                await investigations.request(
                    connection, case, requested_by=investigations.REQUESTED_BY_SERVICE, now=now
                )
    except IntegrityError as error:
        # Another replica opened the case for this component first: join it on the retry.
        raise RetryLaterError("A concurrent change won") from error
    except OperationalError as error:
        raise RetryLaterError(type(error).__name__) from error
    return case is not None
