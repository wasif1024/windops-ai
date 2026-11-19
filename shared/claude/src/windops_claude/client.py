"""One call to Claude under the policy, in the order LLM-001 to LLM-010 need:

1. The budget: no call once the run has reached it, and the output limit lowered to what is left.
2. The role's model and effort, from the policy, with fallback where it is taken.
3. The history guard: the request must repeat the conversation so far exactly.
4. The call. Its usage and cost are recorded whatever the answer, as every call is paid for.
5. The budget again, then the stop reason. Only then is the content read.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from windops_claude import models, responses
from windops_claude.budget import Call, Meter
from windops_claude.conversation import HistoryGuard
from windops_claude.policy import Policy
from windops_claude.pricing import cost_usd
from windops_claude.responses import Response


class StructuredOutputError(ValueError):
    """The answer did not match the schema. It carries the answer, so a repair can append it to
    the conversation as it was, and ask again (M8.8, LLM-008)."""

    def __init__(self, text: str, message: AIMessage | None = None) -> None:
        super().__init__(text)
        self.message = message


@dataclass(frozen=True)
class Result:
    message: AIMessage
    response: Response
    call: Call
    parsed: BaseModel | None = None


class Claude:
    def __init__(
        self,
        policy: Policy,
        meter: Meter,
        api_key: Callable[[], str],
        *,
        inference_geo: str = "global",
        make_model: Callable[..., ChatAnthropic] = models.chat_model,
    ) -> None:
        self.policy, self._meter, self._api_key = policy, meter, api_key
        self._geo, self._make = inference_geo, make_model

    async def invoke(
        self,
        role_name: str,
        messages: Sequence[BaseMessage],
        *,
        schema: type[BaseModel] | None = None,
        guard: HistoryGuard | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> Result:
        role = self.policy.role(role_name)
        allowance = await self._meter.allowance(role.max_tokens)
        model = self._make(self.policy, role_name, api_key=self._api_key(), max_tokens=allowance)
        if guard is not None:
            guard.before(messages)
        parsed: BaseModel | None = None
        parsing_error: Any = None
        if schema is None:
            # Options such as bound tools, as a caller like Deep Agents binds them.
            message = await model.ainvoke(list(messages), **dict(options or {}))
        else:
            answer = await models.structured(model, schema).ainvoke(list(messages))
            message, parsed, parsing_error = (
                answer["raw"],
                answer["parsed"],
                answer["parsing_error"],
            )
        if not isinstance(message, AIMessage):
            raise TypeError("Claude's answer was not a message")
        response = responses.read(message, role.model)
        call = Call(
            role=role_name, requested_model=role.model, served_model=response.served_model,
            effort=role.effort, stop_reason=response.stop_reason, fallback=response.fell_back,
            usage=response.usage,
            cost_usd=cost_usd(self.policy.pricing, response.served_model, response.usage,
                              inference_geo=self._geo),
        )  # fmt: skip
        await self._meter.charge(call)
        responses.check(message, role.model)
        if guard is not None:
            guard.after(message)
        if schema is not None and (parsed is None or parsing_error is not None):
            raise StructuredOutputError(f"The answer did not match {schema.__name__}", message)
        return Result(message=message, response=response, call=call, parsed=parsed)
