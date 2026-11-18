"""Chat models made only from the policy, configured as make vendor-check confirmed (ADR 0015).

- **Model and effort** come from the role's policy entry, never the caller (LLM-001, LLM-003).
  LangChain then sends adaptive thinking, the only mode Opus 5.5 accepts.
- **Refusal fallback** is on for every model that takes it (LLM-006).
- **Structured results** use Claude's structured outputs, `method="json_schema"`, and are
  validated against a Pydantic model. Forced tool choice is never used (LLM-004).
- **Output limit:** a budget may lower max_tokens, never raise it.
"""

from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import LanguageModelInput
from langchain_core.runnables import Runnable
from pydantic import BaseModel, SecretStr

from windops_claude.policy import FALLBACK_MODELS, Policy, Role

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def request_options(role: Role, max_tokens: int | None = None) -> dict[str, Any]:
    """The options a role's calls are made with, as ChatAnthropic takes them."""
    limit = role.max_tokens if max_tokens is None else min(max_tokens, role.max_tokens)
    if limit <= 0:
        raise ValueError("No output tokens left to ask for")
    options: dict[str, Any] = {"model": role.model, "effort": role.effort, "max_tokens": limit}
    if role.model in FALLBACK_MODELS:
        options["betas"] = [FALLBACK_BETA]
        options["model_kwargs"] = {"fallbacks": "default"}
    return options


def chat_model(
    policy: Policy,
    role_name: str,
    *,
    api_key: str,
    max_tokens: int | None = None,
    max_retries: int = 2,
    timeout_seconds: float = 300.0,
) -> ChatAnthropic:
    options = request_options(policy.role(role_name), max_tokens)
    return ChatAnthropic(  # type: ignore[call-arg]
        api_key=SecretStr(api_key),
        max_retries=max_retries,
        default_request_timeout=timeout_seconds,
        **options,
    )


def structured[S: BaseModel](
    model: ChatAnthropic, schema: type[S]
) -> Runnable[LanguageModelInput, Any]:
    """Claude's structured outputs, with the raw message kept so its stop reason is checked first.

    The result is {"raw": AIMessage, "parsed": Schema | None, "parsing_error": Exception | None}.
    """
    return model.with_structured_output(schema, method="json_schema", include_raw=True)
