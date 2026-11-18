"""The Claude model policy: each role's model and effort, the prices, and the budgets (LLM-001).

It is read from claude-policy.yaml, checked, and identified by its version and content hash, which
each run pins (AGT-004). policy.lock.json records the hash of each version, so a change that is
not given a new version fails the tests.
"""

import hashlib
import json
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY_FILE = Path(__file__).with_name("claude-policy.yaml")
LOCK_FILE = Path(__file__).with_name("policy.lock.json")
Effort = Literal["low", "medium", "high", "xhigh", "max"]
# The models that accept server-side refusal fallback. Haiku 5.5 has none, and refuses the field.
FALLBACK_MODELS = frozenset({"claude-opus-5-5"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(_Strict):
    model: str = Field(pattern=r"^claude-[a-z0-9-]+$")
    effort: Effort
    max_tokens: int = Field(gt=0, le=128_000)


class Rates(_Strict):
    """USD per million tokens."""

    input: Decimal = Field(ge=0)
    cache_write_5m: Decimal = Field(ge=0)
    cache_write_1h: Decimal = Field(ge=0)
    cache_read: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)


class LongContext(_Strict):
    above_prompt_tokens: int = Field(gt=0)
    rates: Rates


class Pricing(_Strict):
    source: str
    checked: date
    us_inference_multiplier: Decimal = Field(ge=1)
    models: dict[str, Rates]
    long_context: dict[str, LongContext] = {}

    def highest(self) -> Rates:
        """The highest rate of each kind, for a model with no price of its own."""
        every = list(self.models.values()) + [c.rates for c in self.long_context.values()]
        return Rates(**{k: max(getattr(r, k) for r in every) for k in Rates.model_fields})


class Budget(_Strict):
    tokens: int = Field(gt=0)
    cost_usd: Decimal = Field(gt=0)


class Budgets(_Strict):
    default: Budget
    tenants: dict[str, Budget] = {}


class Policy(_Strict):
    version: int = Field(gt=0)
    roles: dict[str, Role]
    pricing: Pricing
    budgets: Budgets

    @model_validator(mode="after")
    def _priced(self) -> "Policy":
        for name, role in self.roles.items():
            if role.model not in self.pricing.models:
                raise ValueError(f"The {name} role's model has no price: {role.model}")
        return self

    def role(self, name: str) -> Role:
        try:
            return self.roles[name]
        except KeyError:
            raise KeyError(f"No role named {name!r} in the Claude policy") from None

    def budget_for(self, tenant: str) -> Budget:
        return self.budgets.tenants.get(tenant, self.budgets.default)

    @property
    def content_hash(self) -> str:
        return content_hash(self)


def content_hash(policy: Policy) -> str:
    canonical = json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse(text: str) -> Policy:
    return Policy.model_validate(yaml.safe_load(text))


@cache
def load(path: Path = POLICY_FILE) -> Policy:
    return parse(path.read_text())


def locked_hashes(path: Path = LOCK_FILE) -> dict[int, str]:
    return {int(version): value for version, value in json.loads(path.read_text()).items()}


def lock(path: Path = POLICY_FILE, lock_path: Path = LOCK_FILE) -> str:
    """Record the policy's hash for its version. Refuses to change an already locked version."""
    policy = parse(path.read_text())
    hashes = locked_hashes(lock_path) if lock_path.exists() else {}
    known = hashes.get(policy.version)
    if known is not None and known != policy.content_hash:
        raise ValueError(
            f"Version {policy.version} is already locked with other content: give the change "
            "a new version"
        )
    hashes[policy.version] = policy.content_hash
    lock_path.write_text(
        json.dumps({str(v): h for v, h in sorted(hashes.items())}, indent=2) + "\n"
    )
    return policy.content_hash
