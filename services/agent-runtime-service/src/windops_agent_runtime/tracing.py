"""Agent traces in LangSmith, every value redacted before it leaves (OBS-005 to OBS-008, EGR-003).

Nothing traces by itself: LANGSMITH_TRACING stays off, so no library sends anything of its own.
The runtime traces a run only when WINDOPS_AGENT_TRACING is on and its LangSmith key is mounted,
and then through one client whose hooks redact every input, output and metadata value before
export (`windops_observability.redact_trace`). A trace's root run carries the investigation,
tenant, user and correlation IDs; the correlation ID is the run's own, which every audit record
the run causes shares (OBS-008). The Audit Service stays the authoritative record.

Each export is logged by its run and project, never its content (EGR-004).
"""

import contextlib
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import Client
from windops_observability import redact_trace

logger = logging.getLogger("windops.agent_runtime")
# Only this service's pod mounts the key, read when the runtime starts.
KEY_FILE = Path("/var/run/windops/secrets/langsmith/api-key")


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
