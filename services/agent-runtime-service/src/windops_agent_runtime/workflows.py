"""The workflows a run may follow, each pinned by name and version when the run is queued (AGT-004).

A workflow is a fixed sequence of named steps. It reports each one through its context, which
records the step once it completes, and skips any step an earlier attempt completed. So a run that
another replica takes over carries on after its last completed step.

The investigation workflow, on LangGraph and its PostgreSQL checkpoints, comes in M8.4. Until then
the only workflow is `rehearsal`: timed steps, with no model and no tools, so the queue, the
leases and recovery can be proved on the cluster without spending anything.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from windops_grpc import InvalidRequestError


class Context(Protocol):
    async def step(self, name: str, work: Callable[[], Awaitable[None]]) -> None:
        """Run the step, and record it, unless an earlier attempt completed it."""


class Workflow(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def check(self, parameters: Mapping[str, str]) -> dict[str, Any]:
        """Return the parameters, checked and typed, or raise InvalidRequestError."""

    def total_steps(self, parameters: Mapping[str, Any]) -> int: ...

    async def run(self, context: Context, parameters: Mapping[str, Any]) -> None: ...


class StepFailedError(Exception):
    """A step failed. The run is tried again while it has attempts left."""


def _integer(parameters: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = parameters.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a whole number") from error
    if not low <= value <= high:
        raise InvalidRequestError(f"{name} must be from {low} to {high}")
    return value


def _number(
    parameters: Mapping[str, str], name: str, default: float, low: float, high: float
) -> float:
    raw = parameters.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as error:
        raise InvalidRequestError(f"{name} must be a number") from error
    if not low <= value <= high:
        raise InvalidRequestError(f"{name} must be from {low:g} to {high:g}")
    return value


@dataclass(frozen=True)
class Rehearsal:
    """Timed steps. `fail_at_step` makes that step fail on every attempt, to prove the limit."""

    name: str = "rehearsal"
    version: str = "1"

    def check(self, parameters: Mapping[str, str]) -> dict[str, Any]:
        unknown = set(parameters) - {"steps", "step_seconds", "fail_at_step"}
        if unknown:
            raise InvalidRequestError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        steps = _integer(parameters, "steps", 5, 1, 50)
        return {
            "steps": steps,
            "step_seconds": _number(parameters, "step_seconds", 1.0, 0.0, 30.0),
            "fail_at_step": _integer(parameters, "fail_at_step", 0, 0, steps),
        }

    def total_steps(self, parameters: Mapping[str, Any]) -> int:
        return int(parameters["steps"])

    async def run(self, context: Context, parameters: Mapping[str, Any]) -> None:
        seconds = float(parameters["step_seconds"])
        for position in range(1, int(parameters["steps"]) + 1):
            failing = position == parameters["fail_at_step"]

            async def work(failing: bool = failing) -> None:
                await asyncio.sleep(seconds)
                if failing:
                    raise StepFailedError("The rehearsal was asked to fail here")

            await context.step(f"step-{position}", work)


REGISTRY: dict[str, Workflow] = {workflow.name: workflow for workflow in (Rehearsal(),)}


def canonical(parameters: Mapping[str, Any]) -> str:
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"))
