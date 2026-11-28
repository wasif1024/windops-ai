"""Agent traces in LangSmith, every value redacted before it leaves (OBS-005 to OBS-008, EGR-003).

Nothing traces by itself: LANGSMITH_TRACING stays off, so no library sends anything of its own.
The runtime traces a run only when WINDOPS_AGENT_TRACING is on and its LangSmith key is mounted,
and then through one client whose hooks redact every input, output and metadata value before
export (`windops_observability.redact_trace`). A trace's root run carries the investigation,
tenant, user and correlation IDs; the correlation ID is the run's own, which every audit record
the run causes shares (OBS-008). The Audit Service stays the authoritative record.

Each export is logged by its run and project, never its content (EGR-004).

LangSmith down: the client buffers traces and, when a send fails, drops them, and the Audit
Service stays authoritative. Each failed send is counted in `windops_agent_traces_dropped`, and
WindopsAgentTracesDropped alerts (SRS 7.7).
"""

import contextlib
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import Client
from opentelemetry import metrics
from windops_observability import redact_trace

logger = logging.getLogger("windops.agent_runtime")
# Only this service's pod mounts the key, read when the runtime starts.
KEY_FILE = Path("/var/run/windops/secrets/langsmith/api-key")
# Where the LangSmith client reports a send that failed, so the traces in it were dropped.
LANGSMITH_LOGGER = "langsmith.client"
FAILED_SENDS = (
    "Failed to batch ingest runs",
    "Failed to multipart ingest runs",
    "Failed to send compressed multipart ingest",
)
_dropped = metrics.get_meter("windops.agent_runtime").create_counter(
    "windops_agent_traces_dropped",
    description="Sends of agent traces to LangSmith that failed, so their traces were dropped",
)


class DroppedTraces(logging.Handler):
    """Counts the LangSmith client's failed sends, which it only logs."""

    def __init__(self, count: Callable[[], None] = lambda: _dropped.add(1)) -> None:
        super().__init__(logging.WARNING)
        self._count = count

    def emit(self, record: logging.LogRecord) -> None:
        if isinstance(record.msg, str) and record.msg.startswith(FAILED_SENDS):
            self._count()


def count_dropped_traces(handler: logging.Handler | None = None) -> logging.Handler:
    """Count failed sends from now on. Once is enough: a second call adds nothing."""
    target = logging.getLogger(LANGSMITH_LOGGER)
    for existing in target.handlers:
        if isinstance(existing, DroppedTraces):
            return existing
    added = handler or DroppedTraces()
    target.addHandler(added)
    return added


class Tracer:
    def __init__(self, client: Client, project: str) -> None:
        self.client, self.project = client, project

    @contextlib.contextmanager
    def tracing(self, run_id: str, tags: list[str]) -> Iterator[None]:
        """Every runnable inside is traced, as one tree under the run's root."""
        with tracing_v2_enabled(project_name=self.project, client=self.client, tags=tags):
            yield
        logger.info(
            "Exported a run's trace to LangSmith, redacted",
            extra={"run_id": run_id, "project": self.project},
        )

    def flush(self) -> None:
        self.client.flush()


def redacting_client(api_key: str, api_url: str | None = None) -> Client:
    """A LangSmith client that redacts every value before it is sent (EGR-003)."""
    return Client(
        api_key=api_key, api_url=api_url, hide_inputs=redact_trace, hide_outputs=redact_trace,
        hide_metadata=redact_trace,
    )  # fmt: skip


def tracer_from_environment() -> Tracer | None:
    if os.environ.get("WINDOPS_AGENT_TRACING", "false") != "true":
        return None
    try:
        key = KEY_FILE.read_text().strip()
    except OSError:
        key = ""
    if not key:
        logger.warning("Agent tracing is on, but no LangSmith key is mounted: not tracing")
        return None
    project = os.environ.get("WINDOPS_LANGSMITH_PROJECT", "windops-ai")
    count_dropped_traces()
    return Tracer(redacting_client(key), project)


def run_metadata(
    *, run_id: str, case_id: str, snapshot_id: str, tenant_id: str, requested_by: str,
    workflow: str, attempt: int,
) -> dict[str, Any]:  # fmt: skip
    """What a trace's root says about its run (OBS-005, OBS-008)."""
    return {
        "run_id": run_id, "case_id": case_id, "snapshot_id": snapshot_id,
        "tenant_id": tenant_id, "requested_by": requested_by, "correlation_id": run_id,
        "workflow": workflow, "attempt": attempt,
    }  # fmt: skip
