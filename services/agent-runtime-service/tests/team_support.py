"""Scripted stand-ins for the team's tests: models that answer from a script, and tokens."""

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

# Each role's bound tool names, as the last model of that role saw them.
BOUND: dict[str, list[str]] = {}


class Scripted(BaseChatModel):
    """Answers each turn of one role's conversation from a script, and records its tools."""

    role: str
    script: list[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return self.role

    def _get_ls_params(self, stop: Any = None, **kwargs: Any) -> Any:
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = "windops"
        return params

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        BOUND[self.role] = sorted(getattr(t, "name", None) or t["name"] for t in tools)
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None,
                  **kwargs: Any) -> ChatResult:  # fmt: skip
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


class Tokens:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str, tuple[str, ...]]] = []

    async def for_tool(self, agent: str, audience: str, scopes: list[str]) -> str:
        self.asked.append((agent, audience, tuple(scopes)))
        return "agent-token"


def call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


def mcp_names(agents: Any) -> list[str]:
    """Every MCP tool the team's agents may call, by the name the agent calls it."""
    return sorted(
        {
            f"{scope.split('.')[1]}_{scope.split('.')[2]}"
            for agent in agents
            for scope in agent.tools
            if scope.startswith("mcp.")
        }
    )


SPECTRUM_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["sample_rate_hz", "samples"],
    "properties": {"sample_rate_hz": {"type": "number"},
                   "samples": {"type": "array", "items": {"type": "number"}}},
}  # fmt: skip
TREND_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["points"],
    "properties": {"points": {"type": "array", "items": {"type": "object"}}},
}  # fmt: skip


class FakeSandbox:
    """The Agent Sandbox Adapter, answering every task as completed, unless told otherwise."""

    def __init__(self, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.ran: list[tuple[str, str, dict[str, Any], str]] = []

    async def list_tasks(self, tenant: str) -> list[Any]:
        import json

        from windops.agent_sandbox.v1 import agent_sandbox_pb2 as pb

        return [
            pb.Task(name=name, version=1, description=f"{name} (test)",
                    input_schema_json=json.dumps(schema), code_sha256="c" * 64)
            for name, schema in (("vibration_spectrum", SPECTRUM_SCHEMA),
                                 ("trend_fit", TREND_SCHEMA))
        ]  # fmt: skip

    async def run_task(self, tenant: str, task: str, input_json: str, run_id: str) -> Any:
        import hashlib
        import json

        from windops.agent_sandbox.v1 import agent_sandbox_pb2 as pb
        from windops_agent_runtime.team.sandbox import SandboxUnavailableError

        self.ran.append((tenant, task, json.loads(input_json), run_id))
        if self.unavailable:
            raise SandboxUnavailableError("test")
        output = json.dumps({"peaks": [{"frequency_hz": 37.0, "amplitude": 1.0}]})
        return pb.RunTaskResponse(
            status=pb.RUN_STATUS_COMPLETED, output_json=output, task=task, version=1,
            code="print('task')", code_sha256="c" * 64,
            input_sha256=hashlib.sha256(input_json.encode()).hexdigest(),
            output_sha256=hashlib.sha256(output.encode()).hexdigest(), runtime_ms=40, total_ms=900,
            limits=pb.Limits(input_bytes=131072, seconds=30, memory_mb=512, output_bytes=4096),
        )  # fmt: skip


class Recorded:
    """Where a test's Sandbox uses are recorded."""

    def __init__(self) -> None:
        self.uses: list[Any] = []

    async def __call__(self, use: Any) -> None:
        self.uses.append(use)


def toolbox(answers: Any, sandbox: FakeSandbox | None = None) -> Any:
    """A ToolBox whose MCP tools answer from functions, and whose Sandbox is a stand-in."""
    from windops_agent_runtime.team.sandbox import SandboxTasks
    from windops_agent_runtime.team.tools import ToolBox, stand_in

    return ToolBox(stand_in(answers), sandbox=SandboxTasks(sandbox or FakeSandbox()))


def sandbox_run(record: Recorded | None = None, run_id: str = "run_" + "r" * 20) -> Any:
    from windops_agent_runtime.team.sandbox import SandboxRun

    return SandboxRun(run_id, "tenant-a", record or Recorded())
