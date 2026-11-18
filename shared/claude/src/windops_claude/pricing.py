"""What a call cost, from the usage Claude reports and the policy's prices (LLM-010)."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from windops_claude.policy import Pricing, Rates

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Usage:
    """Claude's usage, as reported. input_tokens excludes cache reads and writes."""

    input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
        )

    @property
    def processed_tokens(self) -> int:
        """What counts against a token budget: everything but cache reads."""
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.output_tokens
        )

    @classmethod
    def from_api(cls, usage: Mapping[str, Any]) -> "Usage":
        """Read the API's usage object, as LangChain keeps it in response_metadata["usage"]."""
        writes = usage.get("cache_creation") or {}
        five = int(writes.get("ephemeral_5m_input_tokens") or 0)
        hour = int(writes.get("ephemeral_1h_input_tokens") or 0)
        if not five and not hour:
            # Without the breakdown, every write is charged as the default five-minute kind.
            five = int(usage.get("cache_creation_input_tokens") or 0)
        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            cache_write_5m_tokens=five,
            cache_write_1h_tokens=hour,
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )


def rates_for(pricing: Pricing, model: str, usage: Usage) -> tuple[Rates, bool]:
    """The rates a call is charged at, and whether the model had a price of its own."""
    rates = pricing.models.get(model)
    if rates is None:
        return pricing.highest(), False
    long = pricing.long_context.get(model)
    if long is not None and usage.prompt_tokens > long.above_prompt_tokens:
        return long.rates, True
    return rates, True


def cost_usd(
    pricing: Pricing, model: str, usage: Usage, *, inference_geo: str = "global"
) -> Decimal:
    rates, _ = rates_for(pricing, model, usage)
    total = (
        usage.input_tokens * rates.input
        + usage.cache_write_5m_tokens * rates.cache_write_5m
        + usage.cache_write_1h_tokens * rates.cache_write_1h
        + usage.cache_read_tokens * rates.cache_read
        + usage.output_tokens * rates.output
    ) / MILLION
    if inference_geo == "us":
        total *= pricing.us_inference_multiplier
    # Rounded up, so a budget is never under-counted.
    return total.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
