"""The chat model every agent uses: each call goes through the Claude policy (ADR 0017, 0021).

Deep Agents calls it like any LangChain chat model. Underneath, each call is the windops-claude
client's: the run's budget first, then the role's model and effort with refusal fallback, the
call recorded with its tokens and cost, and the stop reason checked before anything is read.
Forced tool choice is refused (LLM-004).

Its provider reads as "windops", so Deep Agents applies this platform's harness profile to every
agent built on it: no summarization, no default subagent, and no file or shell tools beyond
reading the run's own skills (ADR 0015, ADR 0021).
"""

from collections.abc import Callable, Sequence
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.language_models.base import LangSmithParams
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import ConfigDict, SecretStr
from windops_claude import Claude

PROVIDER = "windops"
_TEMPLATE_KEY = "sk-" + "unused-" + "0" * 8


class MeteredChat(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    # The agent's role in the Claude policy, which sets its model and effort.
    role: str
    claude: Claude

    @property
    def model_name(self) -> str:
        return self.role

    @property
    def _llm_type(self) -> str:
        return "windops-claude"

    def _get_ls_params(self, stop: list[str] | None = None, **kwargs: Any) -> LangSmithParams:
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_provider"] = PROVIDER
        params["ls_model_name"] = self.role
        return params

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if tool_choice not in (None, "auto"):
            # Opus 5.5 refuses forced tool choice, and LLM-004 forbids it.
            raise ValueError("Forced tool choice is never used")
        # Anthropic's own tool formatting, without any of its other settings.
        formatted = ChatAnthropic(  # type: ignore[call-arg]
            model="claude-opus-5-5", api_key=SecretStr(_TEMPLATE_KEY)
        ).bind_tools(tools, **kwargs)
        return self.bind(**getattr(formatted, "kwargs", {}))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("Agents run asynchronously")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        options = dict(kwargs)
        if stop:
            options["stop"] = stop
        result = await self.claude.invoke(self.role, messages, options=options)
        return ChatResult(generations=[ChatGeneration(message=result.message)])
