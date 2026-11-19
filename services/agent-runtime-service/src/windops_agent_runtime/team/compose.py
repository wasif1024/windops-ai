"""Step 10: the Supervisor's report, as a structured result that passes its checks (M8.8).

One call, in the Supervisor's role, through Claude's structured outputs. If the answer does not
parse, or breaks a check, it is sent back with its problems, at most twice (SRS 5.6). Each repair
appends the answer and the problems to the conversation: nothing earlier is ever edited, and the
history guard refuses any request that would (LLM-008). A result still wrong after two repairs
ends the run, for a person to look at.
"""

import json
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, HumanMessage
from windops_claude import Claude, HistoryGuard, StructuredOutputError, stable_system

from windops_agent_runtime.team.definitions import SUPERVISOR
from windops_agent_runtime.team.result import Draft, Seen, problems

REPAIRS = 2
# References listed to the model at most: a run that saw more cites from the first ones.
_LISTED = 400


class InvalidResultError(Exception):
    """The result still broke its checks after every repair."""


@dataclass(frozen=True)
class Composed:
    draft: Draft
    repairs: int


def brief(case_id: str, snapshot_id: str, report: str, seen: Seen) -> str:
    """The case's data, after the cached prompt (LLM-007)."""
    references = sorted(seen.references)[:_LISTED]
    return "\n\n".join(
        [
            f"Case: {case_id}\nSnapshot: {snapshot_id}",
            "References you may cite, exactly as written:\n" + "\n".join(references),
            "Production impact estimates:\n" + json.dumps(seen.estimates, sort_keys=True),
            "The Supervisor's report:\n" + report,
        ]
    )


async def compose(
    claude: Claude,
    prompt: str,
    *,
    case_id: str,
    snapshot_id: str,
    report: str,
    seen: Seen,
    repairs: int = REPAIRS,
) -> Composed:
    guard = HistoryGuard()
    messages: list[BaseMessage] = [
        stable_system(prompt),
        HumanMessage(brief(case_id, snapshot_id, report, seen)),
    ]
    found: list[str] = []
    for attempt in range(repairs + 1):
        try:
            result = await claude.invoke(SUPERVISOR, messages, schema=Draft, guard=guard)
        except StructuredOutputError as error:
            if error.message is None:
                raise
            answer: BaseMessage = error.message
            found = ["The answer did not match the result's schema"]
        else:
            answer = result.message
            draft = result.parsed
            if not isinstance(draft, Draft):
                raise TypeError("Structured outputs gave no draft")
            found = problems(draft, case_id=case_id, snapshot_id=snapshot_id, seen=seen)
            if not found:
                return Composed(draft=draft, repairs=attempt)
        if attempt < repairs:
            listed = "\n".join(f"- {problem}" for problem in found)
            messages = [
                *messages,
                answer,
                HumanMessage(f"Your result has these problems. Answer again, in full:\n{listed}"),
            ]
    shown = "; ".join(found[:5])
    raise InvalidResultError(f"The result broke its checks after {repairs} repairs: {shown}")
