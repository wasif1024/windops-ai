"""What the team does, as metrics for the Agent Operations dashboard (SRS 10.3).

Labels are fixed values only: agent, server, tool, outcome and specialist names, never content.
"""

from opentelemetry import metrics

_meter = metrics.get_meter("windops.agent_runtime")
# Every tool call an agent makes, MCP or Sandbox. The MCP servers count their side too, without
# the agent.
tool_calls = _meter.create_counter(
    "windops_agent_tool_calls", description="Agents' tool calls, by agent, tool and outcome"
)
# Each specialist the Supervisor hands work to.
handoffs = _meter.create_counter(
    "windops_agent_handoffs", description="The Supervisor's handoffs, by the specialist asked"
)
# Responsible AI (SRS 10.3): each result kept, by whether it needs a person, and each problem its
# checks caught in a draft, by kind, as a claim with no support or a reference no tool returned.
results = _meter.create_counter(
    "windops_agent_results",
    description="Results kept, by mode and whether a person must look (escalation)",
)
result_problems = _meter.create_counter(
    "windops_agent_result_problems",
    description="Problems a result's checks caught and sent back, by mode and kind",
)
# Each problem, by a fixed kind: the order matters, the first that matches names it.
PROBLEM_KINDS = (
    ("did not match the result's schema", "invalid_shape"),
    ("cites nothing", "uncited_claim"),
    ("has no support", "unsupported_hypothesis"),
    ("no tool returned", "unknown_reference"),
    ("unknown references", "unknown_reference"),
    ("at least two sources", "one_sided_conflict"),
    ("estimate", "changed_impact"),
    ("must be", "wrong_identifier_or_range"),
    ("Report observed facts", "no_findings"),
)


def kind_of(problem: str) -> str:
    """A problem's kind, as a label: never its text, which may quote evidence."""
    return next((kind for words, kind in PROBLEM_KINDS if words in problem), "other")


def count_problems(found: list[str], mode: str) -> None:
    for problem in found:
        result_problems.add(1, {"mode": mode, "kind": kind_of(problem)})
