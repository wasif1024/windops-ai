"""The Claude policy, offline: no call reaches Claude. make runtime-check makes real calls."""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, SecretStr, ValidationError
from windops_claude import (
    Budget,
    BudgetExhaustedError,
    Claude,
    HistoryEditedError,
    HistoryGuard,
    MemoryLedger,
    Meter,
    RefusedError,
    TruncatedError,
    UnexpectedStopError,
    Usage,
    VolatilePromptError,
    cost_usd,
    load,
    public_content,
    stable_system,
)
from windops_claude import policy as policy_module
from windops_claude.models import FALLBACK_BETA, request_options
from windops_claude.responses import check, read

POLICY = load()
FAKE_KEY = "sk-" + "test-" + "0" * 8


def reply(
    content: Any = "ok",
    *,
    stop: str = "end_turn",
    model: str = "claude-opus-5-5",
    usage: dict[str, Any] | None = None,
    **metadata: Any,
) -> AIMessage:
    return AIMessage(
        content=content,
        response_metadata={
            "model": model, "stop_reason": stop,
            "usage": usage or {"input_tokens": 100, "output_tokens": 50}, **metadata,
        },
    )  # fmt: skip


# --- The policy (LLM-001, LLM-002, LLM-003)


def test_each_role_has_the_srs_model_and_an_explicit_effort() -> None:
    assert {name: (r.model, r.effort) for name, r in POLICY.roles.items()} == {
        "supervisor": ("claude-opus-5-5", "high"),
        "reliability-engineer": ("claude-opus-5-5", "high"),
        "maintenance-planner": ("claude-opus-5-5", "high"),
        "safety-researcher": ("claude-opus-5-5", "high"),
        "scada-analyst": ("claude-opus-5-5", "medium"),
        "production-impact": ("claude-opus-5-5", "medium"),
        "metadata-extraction": ("claude-haiku-5-5", "low"),
        "evaluator": ("claude-opus-5-5", "high"),
    }


def test_the_policy_is_locked_at_its_version() -> None:
    """Changing the policy without a new version, and make claude-policy-lock, fails here."""
    assert policy_module.locked_hashes()[POLICY.version] == POLICY.content_hash


def test_a_locked_version_cannot_be_given_other_content(tmp_path: Path) -> None:
    text = policy_module.POLICY_FILE.read_text()
    source, lock_file = tmp_path / "policy.yaml", tmp_path / "lock.json"
    source.write_text(text)
    policy_module.lock(source, lock_file)
    source.write_text(text.replace("effort: medium", "effort: high"))
    with pytest.raises(ValueError, match="new version"):
        policy_module.lock(source, lock_file)
    source.write_text(
        text.replace("effort: medium", "effort: high").replace("version: 1", "version: 2")
    )
    policy_module.lock(source, lock_file)
    assert set(policy_module.locked_hashes(lock_file)) == {1, 2}


def test_a_role_without_a_price_or_with_an_unknown_field_is_refused() -> None:
    text = policy_module.POLICY_FILE.read_text()
    with pytest.raises(ValidationError, match="no price"):
        policy_module.parse(text.replace("model: claude-haiku-5-5", "model: claude-haiku-9"))
    with pytest.raises(ValidationError):
        policy_module.parse(text.replace("effort: low,", "effort: low, temperature: 0,"))
    with pytest.raises(ValidationError):
        policy_module.parse(text.replace("effort: low", "effort: lowest"))


def test_a_tenant_may_have_its_own_budget() -> None:
    text = policy_module.POLICY_FILE.read_text().replace(
        "tenants: {}", "tenants: {north-sea: {tokens: 500000, cost_usd: 2.50}}"
    )
    changed = policy_module.parse(text)
    assert changed.budget_for("north-sea") == Budget(tokens=500_000, cost_usd=Decimal("2.50"))
    assert changed.budget_for("atlas-wind") == changed.budgets.default


# --- Models (LLM-003, LLM-004, LLM-006)


def test_opus_takes_fallback_and_haiku_does_not() -> None:
    opus = request_options(POLICY.role("supervisor"))
    assert opus["betas"] == [FALLBACK_BETA] and opus["model_kwargs"] == {"fallbacks": "default"}
    haiku = request_options(POLICY.role("metadata-extraction"))
    assert "betas" not in haiku and "model_kwargs" not in haiku


def test_a_budget_lowers_the_output_limit_and_never_raises_it() -> None:
    role = POLICY.role("scada-analyst")
    assert request_options(role, 500)["max_tokens"] == 500
    assert request_options(role, 10**6)["max_tokens"] == role.max_tokens
    with pytest.raises(ValueError):
        request_options(role, 0)


def test_the_request_sends_the_policys_effort_and_fallback() -> None:
    model = ChatAnthropic(api_key=SecretStr(FAKE_KEY), **request_options(POLICY.role("supervisor")))
    payload = model._get_request_payload([HumanMessage("Say OK.")])
    assert payload["model"] == "claude-opus-5-5"
    assert payload["output_config"]["effort"] == "high"
    assert payload["thinking"]["type"] == "adaptive"
    assert payload["fallbacks"] == "default"


class Finding(BaseModel):
    """A finding."""

    claim: str


def test_structured_results_use_structured_outputs_never_forced_tool_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langchain_core.outputs import ChatGeneration, ChatResult
    from windops_claude.models import structured

    model = ChatAnthropic(api_key=SecretStr(FAKE_KEY), **request_options(POLICY.role("supervisor")))
    payloads: list[dict[str, Any]] = []

    def generate(self: ChatAnthropic, messages: list[Any], stop: Any = None,
                 run_manager: Any = None, **kwargs: Any) -> ChatResult:  # fmt: skip
        payloads.append(self._get_request_payload(messages, stop=stop, **kwargs))
        return ChatResult(generations=[ChatGeneration(message=reply('{"claim": "hot"}'))])

    monkeypatch.setattr(ChatAnthropic, "_generate", generate)
    answer = structured(model, Finding).invoke([HumanMessage("x")])
    assert answer["parsed"] == Finding(claim="hot")
    assert answer["raw"].response_metadata["stop_reason"] == "end_turn"
    assert payloads[0]["output_config"]["format"]["type"] == "json_schema"
    assert "tool_choice" not in payloads[0] and "tools" not in payloads[0]


# --- Responses (LLM-005, LLM-006)


@pytest.mark.parametrize("stop", ["end_turn", "tool_use", "stop_sequence"])
def test_a_readable_stop_passes(stop: str) -> None:
    assert check(reply(stop=stop), "claude-opus-5-5").stop_reason == stop


@pytest.mark.parametrize(
    ("stop", "error"),
    [
        ("refusal", RefusedError),
        ("max_tokens", TruncatedError),
        ("pause_turn", UnexpectedStopError),
        ("model_context_window_exceeded", UnexpectedStopError),
        ("", UnexpectedStopError),
    ],
)
def test_any_other_stop_is_raised_before_the_content_is_read(stop: str, error: type) -> None:
    with pytest.raises(error):
        check(reply(stop=stop), "claude-opus-5-5")


def test_a_refusal_names_its_category() -> None:
    with pytest.raises(RefusedError, match="cyber"):
        check(reply(stop="refusal", stop_details={"category": "cyber"}), "claude-opus-5-5")


def test_a_fallback_is_noticed_from_its_block_or_from_usage() -> None:
    block = {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": "claude-opus-4-8"},
    }
    by_block = read(
        reply([block, {"type": "text", "text": "a"}], model="claude-opus-4-8"), "claude-opus-5-5"
    )
    assert by_block.fell_back and by_block.fallbacks[0].to_model == "claude-opus-4-8"
    usage = {"input_tokens": 1, "output_tokens": 1, "iterations": [{"type": "fallback_message"}]}
    sticky = read(reply(model="claude-opus-5", usage=usage), "claude-opus-5-5")
    assert sticky.fell_back and sticky.served_model == "claude-opus-5"
    assert not read(reply(), "claude-opus-5-5").fell_back


# --- Pricing and budgets (LLM-010)


def test_a_call_is_priced_by_each_kind_of_token() -> None:
    usage = Usage.from_api({
        "input_tokens": 1_000, "cache_read_input_tokens": 20_000, "output_tokens": 500,
        "cache_creation": {"ephemeral_5m_input_tokens": 2_000, "ephemeral_1h_input_tokens": 0},
    })  # fmt: skip
    # 1000 x $4 + 20000 x $0.20 + 2000 x $5 + 500 x $20, per million.
    assert cost_usd(POLICY.pricing, "claude-opus-5-5", usage) == Decimal("0.028000")
    assert cost_usd(POLICY.pricing, "claude-opus-5-5", usage, inference_geo="us") == Decimal(
        "0.030800"
    )
    assert usage.processed_tokens == 3_500


def test_haikus_long_prompts_pay_the_higher_rates() -> None:
    short = Usage(input_tokens=100_000, output_tokens=1_000)
    long = Usage(input_tokens=100_001, output_tokens=1_000)
    assert cost_usd(POLICY.pricing, "claude-haiku-5-5", short) == Decimal("0.010500")
    assert cost_usd(POLICY.pricing, "claude-haiku-5-5", long) == Decimal("0.052501")


def test_a_model_with_no_price_is_charged_the_highest_rates() -> None:
    usage = Usage(input_tokens=1_000_000)
    assert cost_usd(POLICY.pricing, "claude-unknown", usage) == Decimal("5.000000")


def test_cache_writes_without_a_breakdown_are_charged_as_five_minute_writes() -> None:
    usage = Usage.from_api({"input_tokens": 0, "cache_creation_input_tokens": 1_000_000})
    assert usage.cache_write_5m_tokens == 1_000_000
    assert cost_usd(POLICY.pricing, "claude-opus-5-5", usage) == Decimal("5.000000")


class FakeModel:
    def __init__(self, answer: AIMessage) -> None:
        self.answer = answer
        self.sent: list[Any] = []
        self.max_tokens = 0

    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        self.sent.append(messages)
        return self.answer


def claude(
    answer: AIMessage, budget: Budget, ledger: MemoryLedger
) -> tuple[Claude, list[FakeModel]]:
    made: list[FakeModel] = []

    def make(policy: Any, role: str, *, api_key: str, max_tokens: int) -> FakeModel:
        model = FakeModel(answer)
        model.max_tokens = max_tokens
        made.append(model)
        return model

    return Claude(POLICY, Meter(budget, ledger), lambda: FAKE_KEY, make_model=make), made  # type: ignore[arg-type]


def test_a_call_is_recorded_with_its_cost_and_the_limit_set_from_what_is_left() -> None:
    ledger = MemoryLedger()
    client, made = claude(reply(), Budget(tokens=1_000, cost_usd=Decimal(1)), ledger)
    result = asyncio.run(client.invoke("scada-analyst", [HumanMessage("x")]))
    assert made[0].max_tokens == 1_000
    assert result.call.cost_usd == Decimal("0.001400")  # 100 x $4 + 50 x $20, per million
    assert ledger.calls[0].role == "scada-analyst" and ledger.calls[0].effort == "medium"


def test_a_refused_call_is_still_recorded_then_raised() -> None:
    ledger = MemoryLedger()
    client, _ = claude(reply(stop="refusal"), Budget(tokens=10_000, cost_usd=Decimal(1)), ledger)
    with pytest.raises(RefusedError):
        asyncio.run(client.invoke("supervisor", [HumanMessage("x")]))
    assert len(ledger.calls) == 1


def test_a_run_over_its_budget_ends_and_makes_no_further_call() -> None:
    ledger = MemoryLedger()
    client, made = claude(reply(), Budget(tokens=200, cost_usd=Decimal(1)), ledger)
    asyncio.run(client.invoke("supervisor", [HumanMessage("x")]))
    with pytest.raises(BudgetExhaustedError, match="tokens"):
        # 150 used, so 50 are left: the call goes out with max_tokens 50 and reaches the budget.
        asyncio.run(client.invoke("supervisor", [HumanMessage("x")]))
    assert made[-1].max_tokens == 50
    with pytest.raises(BudgetExhaustedError):
        asyncio.run(client.invoke("supervisor", [HumanMessage("x")]))
    assert len(made) == 2


def test_the_cost_budget_is_enforced_too() -> None:
    ledger = MemoryLedger()
    client, _ = claude(reply(), Budget(tokens=10**6, cost_usd=Decimal("0.001")), ledger)
    with pytest.raises(BudgetExhaustedError, match=r"\$"):
        asyncio.run(client.invoke("supervisor", [HumanMessage("x")]))


# --- Caching, history and private reasoning (LLM-007, LLM-008, LLM-009)


def test_the_stable_prompt_is_cached_and_refuses_what_changes() -> None:
    system = stable_system("You are the SCADA Analyst. Cite every reading.")
    assert isinstance(system.content, list)
    assert system.content[0]["cache_control"] == {"type": "ephemeral"}  # type: ignore[index]
    for volatile in ("Today is 2026-11-19.", "Case case_abcdefgh12345678.", "At 14:05 the alarm"):
        with pytest.raises(VolatilePromptError):
            stable_system(f"You are the SCADA Analyst. {volatile}")


def test_history_may_only_grow() -> None:
    system = SystemMessage("s")
    first = [system, HumanMessage("q1")]
    answer = AIMessage(content=[{"type": "thinking", "thinking": "t", "signature": "sig"},
                                {"type": "text", "text": "a1"}])  # fmt: skip
    guard = HistoryGuard()
    guard.before(first)
    guard.after(answer)
    guard.before([*first, answer, HumanMessage("q2")])
    edited = AIMessage(content=[{"type": "text", "text": "a1"}])  # thinking dropped
    for request in (
        [system, HumanMessage("q1 changed"), answer, HumanMessage("q2")],
        [*first, edited, HumanMessage("q2")],
        [*first, HumanMessage("q2")],
        [first[1], first[0], answer, HumanMessage("q2")],
    ):
        replay = HistoryGuard()
        replay.before(first)
        replay.after(answer)
        with pytest.raises(HistoryEditedError):
            replay.before(request)


def test_private_reasoning_is_dropped_from_what_is_kept() -> None:
    message = AIMessage(content=[
        {"type": "thinking", "thinking": "private", "signature": "sig"},
        {"type": "redacted_thinking", "data": "x"},
        {"type": "text", "text": "The bearing is overheating."},
    ])  # fmt: skip
    assert public_content(message) == [{"type": "text", "text": "The bearing is overheating."}]
