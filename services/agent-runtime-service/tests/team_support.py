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
