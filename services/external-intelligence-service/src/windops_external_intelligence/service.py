"""The gRPC API: research through Tavily, recorded and assessed, within each tenant's policy.

Every call asks Authorization first, and stays in the caller's tenant. Research needs
knowledge.read, for the wind farm it concerns when it names one (ADR 0013). A search is made only
after its query is made generic (EXT-006), and only within the tenant's policy, daily quota and
rate limit (EXT-005); otherwise it is recorded as unavailable, with the data gap stated, and the
caller carries on (SRS 7.7, EGR-002). Each search and extraction is kept as made, and audited.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import grpc
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.authorization.v1 import authorization_pb2
from windops.external_intelligence.v1 import external_intelligence_pb2 as pb
from windops.external_intelligence.v1 import external_intelligence_pb2_grpc
from windops_auth import (
    STEP_UP_MESSAGE,
    MfaRequiredError,
    authentication_claims,
    require_recent_mfa,
)
from windops_grpc import (
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ServiceClient,
    UnavailableError,
    current_identity,
)
from windops_migrations import in_tenant
from windops_resilience import BreakerOpenError

from windops_external_intelligence import assess, outbound, sources, tavily
from windops_external_intelligence.store import (
    EXTRACTIONS,
    POLICIES,
    RESEARCH,
    KeyReusedError,
    audit,
    remember,
    remembered,
)

READ, MANAGE = "knowledge.read", "knowledge.manage"
DEFAULT_RESULTS, MAXIMUM_RESULTS = 5, 10
MAXIMUM_EXTRACT = 5
DEFAULT_DAILY_LIMIT, MAXIMUM_DAILY_LIMIT = 200, 10000
# Provider calls a minute, per tenant: a runaway loop spends a minute's worth, not a day's.
PER_MINUTE = 20
MAXIMUM_DOMAINS, MAXIMUM_KEY, MAXIMUM_TEXT = 100, 128, 500
# Tavily takes at most 150 domains to leave out.
MAXIMUM_EXCLUDED = 150
DEADLINE_SECONDS = 3.0
_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")
_DOMAIN = re.compile(r"(?!-)[a-z0-9-]{1,63}(?:\.(?!-)[a-z0-9-]{1,63})+")
_RESEARCH_ID = re.compile(r"res_[a-z0-9]{20}")
COMPLETE, UNAVAILABLE = "complete", "unavailable"


class Authorizer(Protocol):
    async def allowed(self, permission: str, wind_farm_id: str = "") -> bool: ...


class Provider(Protocol):
    async def search(
        self,
        query: str,
        *,
        topic: str,
        time_range: str,
        max_results: int,
        include_domains: list[str],
        exclude_domains: list[str],
        country: str = "",
    ) -> tavily.Searched: ...

    async def extract(self, urls: list[str]) -> list[tavily.Extracted]: ...


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, wind_farm_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission, scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id)
        )
        try:
            answer = await self._client.call(
                self._stub.Check, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
            )
        except (grpc.aio.AioRpcError, BreakerOpenError, TimeoutError) as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)


@dataclass(frozen=True)
class Policy:
    enabled: bool
    allowed_domains: tuple[str, ...]
    blocked_domains: tuple[str, ...]
    daily_limit: int
    updated_at: datetime | None
    updated_by: str


def _stamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _actor_type() -> str:
    """A person, or a service acting as itself, such as the Agent Runtime later (SEC-010)."""
    return "user" if current_identity().user_id else "service"


def source_message(stored: dict[str, Any]) -> pb.Source:
    return pb.Source(
        source_id=stored["source_id"],
        url=stored["url"],
        domain=stored["domain"],
        title=stored["title"],
        content=stored["content"],
        score=stored["score"],
        published_at=_stamp(_when(stored["published_at"])),
        retrieved_at=_stamp(_when(stored["retrieved_at"])),
        flags=stored["flags"],
        notes=stored["notes"],
        trusted=stored["trusted"],
    )


def extraction_message(row: Any) -> pb.Extraction:
    return pb.Extraction(
        source_id=row.source_id,
        url=row.url,
        state=row.state,
        content=row.content,
        content_hash=row.content_hash,
        retrieved_at=_stamp(row.retrieved_at),
        flags=json.loads(row.flags),
        notes=json.loads(row.notes),
        error=row.error,
    )


def record_message(row: Any, extractions: Sequence[Any] = ()) -> pb.ResearchRecord:
    return pb.ResearchRecord(
        research_id=row.research_id,
        topic=row.topic,
        state=row.state,
        data_gap=row.data_gap,
        query=row.query,
        outbound_query=row.outbound_query,
        redactions=json.loads(row.redactions),
        wind_farm_id=row.wind_farm_id,
        turbine_id=row.turbine_id,
        region=row.region,
        turbine_class=row.turbine_class,
        provider=row.provider,
        provider_request_id=row.provider_request_id,
        response_seconds=row.response_seconds,
        sources=[source_message(s) for s in json.loads(row.sources)],
        extractions=[extraction_message(e) for e in extractions],
        requested_by=row.requested_by,
        requested_at=_stamp(row.requested_at),
    )


def _policy_message(policy: Policy) -> pb.ResearchPolicy:
    return pb.ResearchPolicy(
        enabled=policy.enabled,
        allowed_domains=list(policy.allowed_domains),
        blocked_domains=list(policy.blocked_domains),
        daily_limit=policy.daily_limit,
        updated_at=_stamp(policy.updated_at),
        updated_by=policy.updated_by,
    )


async def policy_of(connection: AsyncConnection, tenant: str) -> Policy:
    row = (
        await connection.execute(select(POLICIES).where(POLICIES.c.tenant_id == tenant))
    ).one_or_none()
    if row is None:
        return Policy(True, (), (), DEFAULT_DAILY_LIMIT, None, "")
    return Policy(
        row.enabled,
        tuple(json.loads(row.allowed_domains)),
        tuple(json.loads(row.blocked_domains)),
        row.daily_limit,
        row.updated_at,
        row.updated_by,
    )


async def calls_since(connection: AsyncConnection, tenant: str, since: datetime) -> int:
    """Provider calls, searches and extractions together, since a moment."""
    r, e = RESEARCH.c, EXTRACTIONS.c
    searches = (
        await connection.execute(
            select(func.count())
            .select_from(RESEARCH)
            .where(r.tenant_id == tenant, r.provider_called.is_(True), r.requested_at >= since)
        )
    ).scalar_one()
    extractions = (
        await connection.execute(
            select(func.count(func.distinct(e.retrieved_at)))
            .select_from(EXTRACTIONS)
            .where(e.tenant_id == tenant, e.retrieved_at >= since)
        )
    ).scalar_one()
    return int(searches) + int(extractions)


def gap_for(policy: Policy, today: int, minute: int) -> str:
    """Why the provider may not be asked now, or "" when it may."""
    if not policy.enabled:
        return "Web research is turned off for this tenant"
    if today >= policy.daily_limit:
        return f"This tenant's research quota, {policy.daily_limit} calls a day, is used up"
    if minute >= PER_MINUTE:
        return f"This tenant's research rate, {PER_MINUTE} calls a minute, is reached"
    return ""


def domain_policy(policy: Policy) -> tuple[list[str], list[str]]:
    excluded = sorted(assess.BLOCKED | set(policy.blocked_domains))[:MAXIMUM_EXCLUDED]
    return list(policy.allowed_domains), excluded


def permitted(domain: str, policy: Policy) -> bool:
    if assess.under(domain, assess.BLOCKED | set(policy.blocked_domains)):
        return False
    return not policy.allowed_domains or assess.under(domain, set(policy.allowed_domains))


class ExternalIntelligenceService(
    external_intelligence_pb2_grpc.ExternalIntelligenceServiceServicer
):
    def __init__(
        self,
        engine_: AsyncEngine,
        authorizer: Authorizer,
        sources_: sources.Sources,
        provider: Provider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer = engine_, authorizer
        self._sources, self._provider = sources_, provider
        self._now = clock or (lambda: datetime.now(UTC))

    # --- who may do what

    async def _require_read(self, wind_farm_id: str = "") -> tuple[str, str]:
        if not await self._authorizer.allowed(READ, wind_farm_id):
            raise PermissionDeniedError("You do not have permission to do this")
        identity = current_identity()
        return identity.tenant_id, identity.user_id or identity.subject

    async def _require_manage(self) -> tuple[str, str]:
        """Only a person, with knowledge.manage and a recent second factor (SEC-003)."""
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a person can change the research policy")
        if not await self._authorizer.allowed(MANAGE):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP_MESSAGE) from error
        return identity.tenant_id, identity.user_id

    # --- research

    async def Research(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ResearchRequest, context: Any
    ) -> pb.ResearchResponse:
        if request.topic not in assess.TOPICS:
            raise InvalidRequestError(f"topic is one of {', '.join(assess.TOPICS)}")
        limit = request.max_results or DEFAULT_RESULTS
        if not 1 <= limit <= MAXIMUM_RESULTS:
            raise InvalidRequestError(f"max_results is from 1 to {MAXIMUM_RESULTS}")
        for name in ("wind_farm_id", "turbine_id"):
            value = getattr(request, name)
            if value and not _ID.fullmatch(value):
                raise InvalidRequestError(f"{name} is a lowercase ID")
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant = current_identity().tenant_id
        fleet = await self._sources.fleet(tenant)
        farm = request.wind_farm_id
        turbine = fleet.turbines.get(request.turbine_id) if request.turbine_id else None
        if request.turbine_id:
            if turbine is None:
                raise NotFoundError("No such turbine")
            if farm and farm != turbine[0]:
                raise InvalidRequestError("The turbine is not at that wind farm")
            farm = turbine[0]
        if farm and farm not in fleet.sites:
            raise NotFoundError("No such wind farm")
        tenant, caller = await self._require_read(farm)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(
                    connection, tenant, caller, request.idempotency_key, "Research", request
                )
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
        if first is not None:
            return pb.ResearchResponse.FromString(first)
        site = fleet.sites.get(farm) if farm else None
        region = outbound.region(site) if site else ""
        offshore = site.offshore if site else False
        turbine_class = outbound.turbine_class(turbine[1], offshore) if turbine else ""
        try:
            sent = outbound.compose(
                request.query, fleet.identifiers, region_text=region, class_text=turbine_class
            )
        except outbound.QueryRefusedError as error:
            raise InvalidRequestError(str(error)) from error
        now = self._now()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            policy = await policy_of(connection, tenant)
            today = datetime(now.year, now.month, now.day, tzinfo=UTC)
            gap = gap_for(
                policy,
                await calls_since(connection, tenant, today),
                await calls_since(connection, tenant, now - timedelta(minutes=1)),
            )
        found: tavily.Searched | None = None
        if not gap:
            search_topic, time_range, _ = assess.TOPICS[request.topic]
            include, exclude = domain_policy(policy)
            country = outbound.COUNTRIES.get(site.country, "") if site else ""
            try:
                found = await self._provider.search(
                    sent.query,
                    topic=search_topic,
                    time_range=time_range,
                    max_results=limit,
                    include_domains=include,
                    exclude_domains=exclude,
                    country=country,
                )
            except (tavily.ProviderUnavailableError, tavily.ProviderRefusedError) as error:
                gap = f"Web research is unavailable: {error}"
        kept: list[dict[str, Any]] = []
        if found is not None:
            assessed, levels = [], []
            usable = [r for r in found.results if permitted(assess.domain_of(r.url), policy)]
            for result in usable:
                domain = assess.domain_of(result.url)
                text = f"{result.title}\n{result.content}"
                assessed.append(
                    assess.assess(
                        request.topic, result.published_at, result.score, domain, text, now
                    )
                )
                levels.append(assess.warning_level(text))
            if request.topic in ("forecast", "severe_weather"):
                assess.flag_conflicts(levels, assessed)
            for number, (result, judged) in enumerate(zip(usable, assessed, strict=True), 1):
                domain = assess.domain_of(result.url)
                kept.append(
                    {
                        "source_id": f"src_{number}",
                        "url": result.url,
                        "domain": domain,
                        "title": result.title,
                        "content": result.content,
                        "score": round(result.score, 4),
                        "published_at": result.published_at.isoformat()
                        if result.published_at
                        else None,
                        "retrieved_at": now.isoformat(),
                        "flags": judged.flags,
                        "notes": judged.notes,
                        "trusted": assess.under(domain, assess.TRUSTED),
                    }
                )
        research_id = "res_" + uuid.uuid4().hex[:20]
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = (
                await connection.execute(
                    insert(RESEARCH)
                    .values(
                        tenant_id=tenant,
                        research_id=research_id,
                        topic=request.topic,
                        state=UNAVAILABLE if gap else COMPLETE,
                        data_gap=gap,
                        query=request.query,
                        outbound_query=sent.query,
                        redactions=json.dumps(list(sent.redactions)),
                        wind_farm_id=farm,
                        turbine_id=request.turbine_id,
                        region=region,
                        turbine_class=turbine_class,
                        provider=tavily.PROVIDER,
                        provider_request_id=found.request_id if found else "",
                        response_seconds=found.response_seconds if found else 0.0,
                        provider_called=found is not None
                        or gap.startswith("Web research is unavailable"),
                        sources=tavily.canonical(kept),
                        content_hash=found.content_hash if found else "",
                        requested_by=caller,
                        requested_at=now,
                    )
                    .returning(*RESEARCH.c)
                )
            ).one()
            await audit(
                connection,
                tenant_id=tenant,
                actor_type=_actor_type(),
                actor_id=caller,
                action="external_research.searched",
                resource_id=research_id,
                outcome="failed" if gap else "succeeded",
            )
            response = pb.ResearchResponse(research=record_message(row))
            await remember(
                connection,
                tenant_id=tenant,
                user_id=caller,
                key=request.idempotency_key,
                method="Research",
                request=request,
                response=response,
                now=now,
            )
        return response

    async def _research(self, connection: AsyncConnection, tenant: str, research_id: str) -> Any:
        if not _RESEARCH_ID.fullmatch(research_id):
            raise NotFoundError("No such research")
        row = (
            await connection.execute(
                select(RESEARCH).where(
                    RESEARCH.c.tenant_id == tenant, RESEARCH.c.research_id == research_id
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("No such research")
        return row

    async def _readable(self, research_id: str) -> tuple[str, str, Any]:
        """The research, if the caller may read where it was made. Otherwise not found."""
        tenant = current_identity().tenant_id
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row = await self._research(connection, tenant, research_id)
        try:
            tenant, caller = await self._require_read(row.wind_farm_id)
        except PermissionDeniedError as error:
            if row.wind_farm_id:
                raise NotFoundError("No such research") from error
            raise
        return tenant, caller, row

    async def Extract(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ExtractRequest, context: Any
    ) -> pb.ExtractResponse:
        wanted = list(dict.fromkeys(request.source_ids))
        if not 1 <= len(wanted) <= MAXIMUM_EXTRACT:
            raise InvalidRequestError(f"source_ids holds 1 to {MAXIMUM_EXTRACT} sources")
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant, caller, row = await self._readable(request.research_id)
        by_id = {s["source_id"]: s for s in json.loads(row.sources)}
        unknown = [s for s in wanted if s not in by_id]
        if unknown:
            raise InvalidRequestError(f"Not sources of that research: {', '.join(unknown)}")
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(
                    connection, tenant, caller, request.idempotency_key, "Extract", request
                )
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.ExtractResponse.FromString(first)
            policy = await policy_of(connection, tenant)
            now = self._now()
            today = datetime(now.year, now.month, now.day, tzinfo=UTC)
            gap = gap_for(
                policy,
                await calls_since(connection, tenant, today),
                await calls_since(connection, tenant, now - timedelta(minutes=1)),
            )
            e = EXTRACTIONS.c
            attempts = dict(
                (
                    await connection.execute(
                        select(e.source_id, func.max(e.attempt))
                        .where(e.tenant_id == tenant, e.research_id == row.research_id)
                        .group_by(e.source_id)
                    )
                ).all()
            )
        # The policy now, not the one at search time, decides what may be fetched.
        refused = [s for s in wanted if not permitted(by_id[s]["domain"], policy)]
        if refused:
            raise InvalidRequestError(f"The research policy no longer allows: {', '.join(refused)}")
        if gap:
            return pb.ExtractResponse(research_id=row.research_id, state=UNAVAILABLE, data_gap=gap)
        try:
            extracted = await self._provider.extract([by_id[s]["url"] for s in wanted])
        except (tavily.ProviderUnavailableError, tavily.ProviderRefusedError) as error:
            return pb.ExtractResponse(
                research_id=row.research_id,
                state=UNAVAILABLE,
                data_gap=f"Web research is unavailable: {error}",
            )
        rows = []
        for source_id, page in zip(wanted, extracted, strict=True):
            source = by_id[source_id]
            published = page.published_at or _when(source["published_at"])
            judged = assess.assess(
                row.topic, published, source["score"], source["domain"], page.content, now
            )
            rows.append(
                {
                    "tenant_id": tenant,
                    "research_id": row.research_id,
                    "source_id": source_id,
                    "attempt": attempts.get(source_id, 0) + 1,
                    "url": source["url"],
                    "state": "failed" if page.error else "extracted",
                    "content": page.content,
                    "content_hash": hashlib.sha256(page.content.encode()).hexdigest(),
                    "flags": json.dumps(judged.flags if not page.error else []),
                    "notes": json.dumps(judged.notes if not page.error else []),
                    "error": page.error,
                    "extracted_by": caller,
                    "retrieved_at": now,
                }
            )
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            stored = (
                await connection.execute(insert(EXTRACTIONS).returning(*EXTRACTIONS.c), rows)
            ).all()
            await audit(
                connection,
                tenant_id=tenant,
                actor_type=_actor_type(),
                actor_id=caller,
                action="external_research.extracted",
                resource_id=row.research_id,
            )
            response = pb.ExtractResponse(
                research_id=row.research_id,
                state=COMPLETE,
                extractions=[extraction_message(r) for r in stored],
            )
            await remember(
                connection,
                tenant_id=tenant,
                user_id=caller,
                key=request.idempotency_key,
                method="Extract",
                request=request,
                response=response,
                now=now,
            )
        return response

    async def GetResearch(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetResearchRequest, context: Any
    ) -> pb.GetResearchResponse:
        tenant, _, row = await self._readable(request.research_id)
        e = EXTRACTIONS.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            extractions = (
                await connection.execute(
                    select(EXTRACTIONS)
                    .where(e.tenant_id == tenant, e.research_id == row.research_id)
                    .order_by(e.source_id, e.attempt)
                )
            ).all()
        return pb.GetResearchResponse(research=record_message(row, extractions))

    # --- the policy

    async def GetResearchPolicy(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetResearchPolicyRequest, context: Any
    ) -> pb.GetResearchPolicyResponse:
        tenant, _ = await self._require_read()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            found = await policy_of(connection, tenant)
        return pb.GetResearchPolicyResponse(policy=_policy_message(found))

    async def SetResearchPolicy(  # noqa: N802  # The name comes from the contract.
        self, request: pb.SetResearchPolicyRequest, context: Any
    ) -> pb.SetResearchPolicyResponse:
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")
        if not 1 <= request.daily_limit <= MAXIMUM_DAILY_LIMIT:
            raise InvalidRequestError(f"daily_limit is from 1 to {MAXIMUM_DAILY_LIMIT}")
        lists = {}
        for name in ("allowed_domains", "blocked_domains"):
            values = sorted(
                {d.strip().lower().removeprefix("www.") for d in getattr(request, name)}
            )
            if len(values) > MAXIMUM_DOMAINS or not all(_DOMAIN.fullmatch(d) for d in values):
                raise InvalidRequestError(f"{name} holds at most {MAXIMUM_DOMAINS} domain names")
            lists[name] = values
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant, user = await self._require_manage()
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(
                    connection, tenant, user, request.idempotency_key, "SetResearchPolicy", request
                )
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.SetResearchPolicyResponse.FromString(first)
            columns = {
                "enabled": request.enabled,
                "daily_limit": request.daily_limit,
                "allowed_domains": json.dumps(lists["allowed_domains"]),
                "blocked_domains": json.dumps(lists["blocked_domains"]),
                "updated_at": now,
                "updated_by": user,
            }
            p = POLICIES.c
            if (await connection.execute(select(p.tenant_id).where(p.tenant_id == tenant))).first():
                await connection.execute(
                    update(POLICIES).where(p.tenant_id == tenant).values(**columns)
                )
            else:
                await connection.execute(insert(POLICIES).values(tenant_id=tenant, **columns))
            await audit(
                connection,
                tenant_id=tenant,
                actor_type="user",
                actor_id=user,
                action="external_research.policy_changed",
                resource_type="research_policy",
                resource_id=tenant,
            )
            response = pb.SetResearchPolicyResponse(
                policy=_policy_message(await policy_of(connection, tenant))
            )
            await remember(
                connection,
                tenant_id=tenant,
                user_id=user,
                key=request.idempotency_key,
                method="SetResearchPolicy",
                request=request,
                response=response,
                now=now,
            )
        return response
