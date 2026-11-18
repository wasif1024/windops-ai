"""Each investigation's token and cost budget, enforced around every call (LLM-010).

Before a call, a run that has reached either budget makes no call. Otherwise the call's output
limit is lowered to the tokens left, so one call cannot run far past the budget. After the call,
what it used is recorded, and a run now at or over either budget ends as budget_exhausted.
The overshoot is at most one call's input.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from windops_claude.policy import Budget
from windops_claude.pricing import Usage


class BudgetExhaustedError(Exception):
    """The run has reached its token or cost budget."""


@dataclass(frozen=True)
class Spent:
    tokens: int = 0
    cost_usd: Decimal = Decimal(0)


@dataclass(frozen=True)
class Call:
    """One call to Claude, as it is recorded."""

    role: str
    requested_model: str
    served_model: str
    effort: str
    stop_reason: str
    fallback: bool
    usage: Usage
    cost_usd: Decimal


class Ledger(Protocol):
    async def spent(self) -> Spent: ...

    async def record(self, call: Call) -> Spent: ...


def exhausted(budget: Budget, spent: Spent) -> str | None:
    """Why the budget is used up, or None."""
    if spent.tokens >= budget.tokens:
        return f"Used {spent.tokens} of its {budget.tokens} tokens"
    if spent.cost_usd >= budget.cost_usd:
        return f"Used ${spent.cost_usd:.4f} of its ${budget.cost_usd:.4f}"
    return None


class Meter:
    def __init__(self, budget: Budget, ledger: Ledger) -> None:
        self.budget, self._ledger = budget, ledger

    async def allowance(self, max_tokens: int) -> int:
        """The output limit for the next call, or raise if nothing is left."""
        spent = await self._ledger.spent()
        reason = exhausted(self.budget, spent)
        if reason is not None:
            raise BudgetExhaustedError(reason)
        return max(1, min(max_tokens, self.budget.tokens - spent.tokens))

    async def charge(self, call: Call) -> Spent:
        """Record the call. Raise if the run is now at or over its budget."""
        spent = await self._ledger.record(call)
        reason = exhausted(self.budget, spent)
        if reason is not None:
            raise BudgetExhaustedError(reason)
        return spent


class MemoryLedger:
    """For tests and checks. The Agent Runtime keeps its ledger in PostgreSQL."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    async def spent(self) -> Spent:
        return Spent(
            tokens=sum(c.usage.processed_tokens for c in self.calls),
            cost_usd=sum((c.cost_usd for c in self.calls), Decimal(0)),
        )

    async def record(self, call: Call) -> Spent:
        self.calls.append(call)
        return await self.spent()
