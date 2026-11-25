"""The two stale rules that must be polled: condition drift and weather (SRS 8.5, M10.3b).

Nothing announces either, so every few minutes this asks, as the Investigation Service, about each
case whose newest evidence is still fresh, until its work is done:

- **drift:** the case's own fault rules, run again now by the Fault Detection Service, against
  the figures its snapshot's detections froze (staleness.drift);
- **weather:** for each proposal submitted on that snapshot, which plans work at height in a
  window not yet over, the Policy Service's work-window rules asked again, against their outcome
  at submission (staleness.weather).

**What a weather check costs:** each asking is a Policy result, kept and audited. So one is made
at most once an hour for each proposal, and again sooner only when the work-window rules change,
by an idempotency key over the proposal, the hour and those rules' versions. A forecast revised
within the hour is judged at the next.

**When an owner is down:** the case is judged at the next pass, never without the answer (SRS
7.7). One case failing never holds up the others.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from windops_grpc import DomainError, acting_as_service
from windops_migrations import across_tenants, in_tenant

from windops_investigation import staleness
from windops_investigation.cases import mark_stale
from windops_investigation.stale import RULES, _frozen, _proposals
from windops_investigation.store import (
    APPROVED,
    CASES,
    INVESTIGATING,
    OPEN,
    PENDING_REVIEW,
    SERVICE_NAME,
)

logger = logging.getLogger("windops.service")
EVERY_SECONDS = 300.0
# Until the work is done: afterwards its verification judges the turbine (M10.6).
WATCHED = (OPEN, INVESTIGATING, PENDING_REVIEW, APPROVED)


class Owners(Protocol):
    """Asked as this service, in the case's tenant."""

    async def evaluate(self, turbine_id: str, at: datetime) -> Any: ...
    async def policy_rules(self) -> list[Any]: ...
    async def policy_result(self, result_id: str) -> Any: ...
    async def evaluate_proposal(
        self, case_id: str, proposal: Any, impact_result_id: str, key: str
    ) -> Any: ...


def frozen_figures(detections: Sequence[Any]) -> dict[str, staleness.Figure]:
    """Each rule's figure from the snapshot's detection items: the newest detection's."""
    newest: dict[str, tuple[str, staleness.Figure]] = {}
    for item in detections:
        content = json.loads(item.content)
        rule_id, at = content.get("rule_id", ""), content.get("detected_at", "")
        if rule_id and (rule_id not in newest or at > newest[rule_id][0]):
            figure = staleness.Figure(content.get("kind", ""), float(content.get("amount", 0.0)))
            newest[rule_id] = (at, figure)
    return {rule_id: figure for rule_id, (_, figure) in newest.items()}


def weather_key(work_order_id: str, version: int, now: datetime, rules: Sequence[Any]) -> str:
    """The same within the hour while the work-window rules stand, so Policy answers once."""
    versions = sorted(
        (r.rule_id, r.current.version, r.enabled)
        for r in rules if r.category == staleness.WEATHER_CATEGORY
    )  # fmt: skip
    digest = hashlib.sha256(json.dumps([work_order_id, version, versions]).encode()).hexdigest()
    return f"stale-weather-{now:%Y%m%d%H}-{digest[:24]}"


def _window_rules(result: Any) -> list[Any]:
    return [r for r in result.rules if r.category == staleness.WEATHER_CATEGORY]


class Watcher:
    def __init__(self, engine: AsyncEngine, owners: Owners, secret: str) -> None:
        self.engine, self.owners, self.secret = engine, owners, secret

    async def run_once(self, now: datetime | None = None) -> int:
        """Judge every fresh active case once. Returns how many went stale."""
        now = now or datetime.now(UTC)
        c = CASES.c
        async with self.engine.connect() as connection:
            await across_tenants(connection)
            watched = (await connection.execute(
                select(c.tenant_id, c.case_id).where(
                    c.status.in_(WATCHED), c.evidence_snapshot_id != "", c.stale_rule == "")
                .order_by(c.tenant_id, c.case_id)
            )).all()  # fmt: skip
        marked, rules_of = 0, dict[str, list[Any]]()
        for tenant, case_id in watched:
            try:
                with acting_as_service(SERVICE_NAME, tenant, self.secret):
                    found = await self._judge(tenant, case_id, now, rules_of)
            except DomainError as error:
                logger.warning(
                    "Could not judge a case's drift or weather yet",
                    extra={"case_id": case_id, "error_type": type(error).__name__},
                )
                continue
            if found is not None and await self._mark(tenant, case_id, found, now):
                marked += 1
        return marked

    async def _judge(self, tenant: str, case_id: str, now: datetime,
                     rules_of: dict[str, list[Any]]) -> staleness.Stale | None:  # fmt: skip
        async with self.engine.connect() as connection:
            await in_tenant(connection, tenant)
            case = (await connection.execute(select(CASES).where(
                CASES.c.tenant_id == tenant, CASES.c.case_id == case_id))).one()  # fmt: skip
            detections = await _frozen(connection, tenant, case.evidence_snapshot_id, "detection")
            proposals = [p for p in await _proposals(connection, tenant, case)
                         if p.policy_result_id]  # fmt: skip
        frozen = frozen_figures(detections)
        if any(f.kind in staleness.DRIFT_KINDS for f in frozen.values()):
            evaluated = await self.owners.evaluate(case.turbine_id, now)
            now_found = {f.rule_id: staleness.Figure(f.kind, f.amount) for f in evaluated.findings}
            found = staleness.drift(frozen, now_found)
            if found is not None:
                return found
        for proposal in proposals:
            found = await self._weather(case, proposal, now, rules_of, tenant)
            if found is not None:
                return found
        return None

    async def _weather(
        self, case: Any, proposal: Any, now: datetime, rules_of: dict[str, list[Any]],
        tenant: str,
    ) -> staleness.Stale | None:  # fmt: skip
        submitted = await self.owners.policy_result(proposal.policy_result_id)
        planned = submitted.proposal
        before = {r.rule_id: r.outcome for r in _window_rules(submitted)}
        if not planned.requires_climb or not planned.HasField("planned_end"):
            return None
        if planned.planned_end.ToDatetime(tzinfo=UTC) <= now:
            return None  # The window is over: there is no forecast left to judge it by.
        if tenant not in rules_of:
            rules_of[tenant] = await self.owners.policy_rules()
        key = weather_key(proposal.work_order_id, proposal.version, now, rules_of[tenant])
        again = await self.owners.evaluate_proposal(case.case_id, planned,
                                                    proposal.impact_result_id, key)  # fmt: skip
        after = [(r.rule_id, r.outcome, r.reason) for r in _window_rules(again)]
        return staleness.weather(before, after, proposal.work_order_id)

    async def _mark(self, tenant: str, case_id: str, found: staleness.Stale,
                    now: datetime) -> bool:  # fmt: skip
        async with self.engine.begin() as connection:
            await in_tenant(connection, tenant)
            case = (await connection.execute(select(CASES).where(
                CASES.c.tenant_id == tenant, CASES.c.case_id == case_id)
                .with_for_update())).one()  # fmt: skip
            if case.stale_rule or case.status not in WATCHED:
                return False
            await mark_stale(connection, case, found, now=now, by=RULES)
        logger.info("Evidence went stale", extra={"case_id": case_id, "rule": found.rule})
        return True


async def keep_watching(watcher: Watcher, stop: asyncio.Event,
                        every_seconds: float = EVERY_SECONDS) -> None:  # fmt: skip
    while not stop.is_set():
        try:
            await watcher.run_once()
        except Exception as error:  # Tried again on the next pass.
            logger.warning("Could not watch for drift and weather",
                           extra={"error_type": type(error).__name__})  # fmt: skip
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_seconds)
