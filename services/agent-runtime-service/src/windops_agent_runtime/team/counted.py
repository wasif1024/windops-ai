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
