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

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from windops_claude import Claude, stable_system
from windops_grpc import InvalidRequestError


class Context(Protocol):
    # Every call through it follows the Claude policy and is metered against the run's budget.
    # None when the runtime has no key, or the run has no budget.
    claude: Claude | None

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


class ClaudeUnavailableError(Exception):
    """The run needs Claude, and the runtime has no key for it, or the run no budget."""


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


# Long enough to be cached: Opus 5.5 caches a prompt of 512 tokens or more. It never changes.
_STABLE = "\n".join(
    ["You are part of a check of the WindOps AI runtime. Follow every rule below."]
    + [f"Rule {n}: a turbine reading marked as suspect quality is never used as evidence on its "
       f"own, and rule {n} applies to every component group equally." for n in range(1, 121)]
)  # fmt: skip


class Readiness(BaseModel):
    """Whether the agent is ready."""

    ready: bool = Field(description="True when ready")
    role: str = Field(description="The role named in the question")


@dataclass(frozen=True)
class ModelCheck:
    """One short call per role, a structured result, and a cached prompt read back. A few cents
    of synthetic text, so the policy can be proved against Claude itself."""

    name: str = "model-check"
    version: str = "1"

    def check(self, parameters: Mapping[str, str]) -> dict[str, Any]:
        from windops_claude import load

        unknown = set(parameters) - {"roles"}
        if unknown:
            raise InvalidRequestError(f"Unknown parameters: {', '.join(sorted(unknown))}")
        known = load().roles
        roles = [r for r in parameters.get("roles", ",".join(known)).split(",") if r]
        if not roles or any(r not in known for r in roles):
            raise InvalidRequestError(f"roles must be from: {', '.join(known)}")
        return {"roles": roles}

    def total_steps(self, parameters: Mapping[str, Any]) -> int:
        return len(parameters["roles"]) + 2

    async def run(self, context: Context, parameters: Mapping[str, Any]) -> None:
        claude = context.claude
        if claude is None:
            raise ClaudeUnavailableError("The runtime has no Anthropic key, or the run no budget")
        system = stable_system(_STABLE)
        for role in parameters["roles"]:

            async def ask(role: str = role) -> None:
                result = await claude.invoke(
                    role, [system, HumanMessage(f"You are the {role}. Reply with one word: ready.")]
                )
                if not result.message.text.strip():
                    raise StepFailedError(f"The {role} gave no answer")

            await context.step(f"ask-{role}", ask)

        async def structured() -> None:
            result = await claude.invoke(
                "supervisor",
                [system, HumanMessage("Are you ready? You are the supervisor.")],
                schema=Readiness,
            )
            if not isinstance(result.parsed, Readiness):
                raise StepFailedError("No structured answer")

        await context.step("structured", structured)

        async def cached() -> None:
            # The same stable prompt again: read from the cache this time (LLM-007).
            await claude.invoke("supervisor", [system, HumanMessage("Reply with one word: ready.")])

        await context.step("cached", cached)


REGISTRY: dict[str, Workflow] = {
    workflow.name: workflow for workflow in (Rehearsal(), ModelCheck())
}


def canonical(parameters: Mapping[str, Any]) -> str:
    return json.dumps(parameters, sort_keys=True, separators=(",", ":"))
