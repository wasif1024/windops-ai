"""The gRPC API: policy rules, each version kept, and judging a proposal (FR-POL-001 to 005).

Every call asks Authorization first, and stays in the caller's tenant. Evaluating a proposal
checks that the caller may read the case's turbine, then gathers every fact as the service itself
(ADR 0009), judges every enabled rule, and records the result, which never changes.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from google.protobuf import json_format
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.authorization.v1 import authorization_pb2
from windops.policy.v1 import policy_pb2 as pb
from windops.policy.v1 import policy_pb2_grpc
from windops_auth import (
    STEP_UP_MESSAGE,
    MfaRequiredError,
    authentication_claims,
    require_recent_mfa,
)
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ServiceClient,
    UnavailableError,
    acting_as_service,
    current_identity,
)
from windops_migrations import in_tenant

from windops_policy import library, rules, sources
from windops_policy.defaults import LIBRARY
from windops_policy.store import (
    POLICY_RESULTS,
    POLICY_RULE_VERSIONS,
    POLICY_RULES,
    SERVICE_NAME,
    KeyReusedError,
    audit,
    remember,
    remembered,
)

READ, MANAGE = "policy.read", "policy.manage"
# A result judges a case's proposal, so those who may read the case may ask for and read one.
READ_CASES = "investigation.read"
RESULT_FORMAT = "windops-policy-result-v1"
DEFAULT_PAGE, MAXIMUM_PAGE = 50, 200
MAXIMUM_KEY, MAXIMUM_TEXT, MAXIMUM_DEFINITION = 64, 500, 8000
_RULE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
_RESULT_ID = re.compile(r"pol_[a-z0-9]{20}")
_IMPACT_ID = re.compile(r"imp_[a-z0-9]{20}")


class Authorizer(Protocol):
    async def allowed(
        self, permission: str, wind_farm_id: str = "", turbine_id: str = ""
    ) -> bool: ...


class AuthorizationClient:
    """Asks the Authorization Service, as the caller. Any failure denies (SRS 7.7)."""

    def __init__(self, stub: Any, client: ServiceClient) -> None:
        self._stub, self._client = stub, client

    async def allowed(self, permission: str, wind_farm_id: str = "", turbine_id: str = "") -> bool:
        request = authorization_pb2.CheckRequest(
            permission=permission,
            scope=authorization_pb2.Scope(wind_farm_id=wind_farm_id, turbine_id=turbine_id),
        )
        try:
            answer = await sources.ask(self._client, self._stub.Check, request, "Authorization")
        except NotFoundError as error:
            raise UnavailableError("Permissions cannot be checked right now") from error
        return bool(answer.allowed)


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def _version(row: Any) -> pb.PolicyRuleVersion:
    return pb.PolicyRuleVersion(
        rule_id=row.rule_id, version=row.version, kind=row.kind, definition_json=row.definition,
        content_hash=row.content_hash, created_by=row.created_by,
        created_at=_timestamp(row.created_at), reason=row.reason,
    )  # fmt: skip


def _definition(text: str) -> dict[str, Any]:
    if len(text) > MAXIMUM_DEFINITION:
        raise InvalidRequestError(f"definition_json is at most {MAXIMUM_DEFINITION} characters")
    try:
        value = json.loads(text)
    except ValueError as error:
        raise InvalidRequestError("definition_json is not JSON") from error
    if not isinstance(value, dict):
        raise InvalidRequestError("definition_json must be an object")
    return value


def _content(result: pb.PolicyResult) -> str:
    """The result as canonical JSON, without the fields that describe it."""
    plain = pb.PolicyResult()
    plain.CopyFrom(result)
    plain.content_hash, plain.verified = "", False
    data = json_format.MessageToDict(plain, preserving_proto_field_name=True)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def result_hash(content: str) -> str:
    return hashlib.sha256(f"{RESULT_FORMAT}|{content}".encode()).hexdigest()


def _from_row(row: Any) -> pb.PolicyResult:
    result = json_format.ParseDict(json.loads(row.content), pb.PolicyResult())
    result.content_hash = row.content_hash
    # Read back, the result is hashed again: a changed row no longer verifies.
    result.verified = result_hash(row.content) == row.content_hash
    return result


class PolicyService(policy_pb2_grpc.PolicyServiceServicer):
    def __init__(
        self,
        engine_: AsyncEngine,
        authorizer: Authorizer,
        sources_: sources.Sources,
        secret: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer, self._sources = engine_, authorizer, sources_
        self._secret = secret
        self._now = clock or (lambda: datetime.now(UTC))

    # --- who may do what

    async def _require_read(self) -> str:
        if not await self._authorizer.allowed(READ):
            raise PermissionDeniedError("You do not have permission to do this")
        return current_identity().tenant_id

    async def _require_manage(self) -> tuple[str, str]:
        """Only a person, with policy.manage and a recent second factor (SEC-003)."""
        identity = current_identity()
        if not identity.user_id:
            raise PermissionDeniedError("Only a person can change policy rules")
        if not await self._authorizer.allowed(MANAGE):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP_MESSAGE) from error
        return identity.tenant_id, identity.user_id

    async def _may_read_case(self, turbine_id: str, wind_farm_id: str) -> bool:
        """For the whole tenant, or this turbine or its wind farm."""
        if await self._authorizer.allowed(READ_CASES):
            return True
        return bool(turbine_id) and await self._authorizer.allowed(
            READ_CASES, wind_farm_id, turbine_id
        )

    # --- the rules

    async def _install_defaults(self, tenant: str) -> None:
        await library.install_defaults(self._engine, tenant, self._now())

    async def _rules(self, connection: AsyncConnection, tenant: str) -> list[pb.PolicyRule]:
        r, v = POLICY_RULES.c, POLICY_RULE_VERSIONS.c
        rows = (
            await connection.execute(
                select(POLICY_RULES, POLICY_RULE_VERSIONS)
                .join(
                    POLICY_RULE_VERSIONS,
                    (v.tenant_id == r.tenant_id)
                    & (v.rule_id == r.rule_id)
                    & (v.version == r.current_version),
                )
                .where(r.tenant_id == tenant)
                .order_by(r.rule_id)
            )
        ).all()
        return [
            pb.PolicyRule(
                rule_id=row.rule_id,
                name=row.name,
                description=row.description,
                category=row.category,
                enabled=row.enabled,
                current=_version(row),
            )
            for row in rows
        ]

    async def _rule(self, connection: AsyncConnection, tenant: str, rule_id: str) -> pb.PolicyRule:
        found = [rule for rule in await self._rules(connection, tenant) if rule.rule_id == rule_id]
        if not found:
            raise NotFoundError("No such policy rule")
        return found[0]

    async def ListPolicyRules(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListPolicyRulesRequest, context: Any
    ) -> pb.ListPolicyRulesResponse:
        tenant = await self._require_read()
        await self._install_defaults(tenant)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            return pb.ListPolicyRulesResponse(
                rules=await self._rules(connection, tenant), library=LIBRARY
            )

    async def GetPolicyRule(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetPolicyRuleRequest, context: Any
    ) -> pb.GetPolicyRuleResponse:
        tenant = await self._require_read()
        await self._install_defaults(tenant)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rule = await self._rule(connection, tenant, request.rule_id)
            v = POLICY_RULE_VERSIONS.c
            versions = (
                await connection.execute(
                    select(POLICY_RULE_VERSIONS)
                    .where(v.tenant_id == tenant, v.rule_id == request.rule_id)
                    .order_by(v.version)
                )
            ).all()
        return pb.GetPolicyRuleResponse(rule=rule, versions=[_version(row) for row in versions])

    async def _change(
        self, method: str, request: Message, key: str,
        act: Callable[[AsyncConnection, str, str, datetime], Awaitable[pb.PolicyRule]],
    ) -> Any:  # fmt: skip
        if len(key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        tenant, user = await self._require_manage()
        await self._install_defaults(tenant)
        response_type = {
            "PublishPolicyRuleVersion": pb.PublishPolicyRuleVersionResponse,
            "SetPolicyRuleEnabled": pb.SetPolicyRuleEnabledResponse,
        }[method]
        now = self._now()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                try:
                    first = await remembered(connection, tenant, user, key, method, request)
                except KeyReusedError as error:
                    raise InvalidRequestError(str(error)) from error
                if first is not None:
                    return response_type.FromString(first)
                response = response_type(rule=await act(connection, tenant, user, now))
                await remember(
                    connection, tenant_id=tenant, user_id=user, key=key, method=method,
                    request=request, response=response, now=now,
                )  # fmt: skip
        except IntegrityError as error:
            raise ConflictError(
                "Someone else changed this rule first. Read it, and try again"
            ) from error
        return response

    async def PublishPolicyRuleVersion(  # noqa: N802  # The name comes from the contract.
        self, request: pb.PublishPolicyRuleVersionRequest, context: Any
    ) -> pb.PublishPolicyRuleVersionResponse:
        if not _RULE_ID.fullmatch(request.rule_id):
            raise InvalidRequestError("rule_id is 3 to 64 lowercase letters, digits and hyphens")
        reason, name = request.reason.strip(), request.name.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")
        if len(name) > MAXIMUM_TEXT or len(request.description) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"name and description are at most {MAXIMUM_TEXT} characters")
        definition = _definition(request.definition_json)
        try:
            rules.parse(request.rule_id, 1, name, definition)
        except rules.RuleError as error:
            raise InvalidRequestError(f"The rule cannot be used: {error}") from error

        async def act(connection: AsyncConnection, tenant: str, user: str, now: datetime) -> Any:
            r = POLICY_RULES.c
            current = (
                await connection.execute(
                    select(POLICY_RULES).where(r.tenant_id == tenant, r.rule_id == request.rule_id)
                )
            ).one_or_none()
            have = current.current_version if current is not None else 0
            if have != request.expected_version:
                raise ConflictError(
                    f"This rule is at version {have}, not {request.expected_version}. Read it, "
                    "and try again"
                )
            version = have + 1
            await library.write_version(connection, tenant, request.rule_id, version, definition,
                                        user, reason, now)  # fmt: skip
            if current is None:
                if not name:
                    raise InvalidRequestError("A new rule needs a name")
                await connection.execute(
                    POLICY_RULES.insert().values(
                        tenant_id=tenant, rule_id=request.rule_id, name=name,
                        description=request.description.strip(), enabled=True,
                        current_version=version, created_at=now, updated_at=now,
                    )
                )  # fmt: skip
                action = "policy_rule.created"
            else:
                await connection.execute(
                    update(POLICY_RULES)
                    .where(r.tenant_id == tenant, r.rule_id == request.rule_id)
                    .values(
                        current_version=version, updated_at=now, name=name or current.name,
                        description=request.description.strip() or current.description,
                    )
                )  # fmt: skip
                action = "policy_rule.version_published"
            await audit(connection, tenant_id=tenant, actor_type="user", actor_id=user,
                        action=action, resource_id=request.rule_id)  # fmt: skip
            return await self._rule(connection, tenant, request.rule_id)

        response: pb.PublishPolicyRuleVersionResponse = await self._change(
            "PublishPolicyRuleVersion", request, request.idempotency_key, act
        )
        return response

    async def SetPolicyRuleEnabled(  # noqa: N802  # The name comes from the contract.
        self, request: pb.SetPolicyRuleEnabledRequest, context: Any
    ) -> pb.SetPolicyRuleEnabledResponse:
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required, and at most {MAXIMUM_TEXT} characters")

        async def act(connection: AsyncConnection, tenant: str, user: str, now: datetime) -> Any:
            rule = await self._rule(connection, tenant, request.rule_id)
            r = POLICY_RULES.c
            await connection.execute(
                update(POLICY_RULES)
                .where(r.tenant_id == tenant, r.rule_id == request.rule_id)
                .values(enabled=request.enabled, updated_at=now)
            )
            if rule.enabled != request.enabled:
                action = "policy_rule.enabled" if request.enabled else "policy_rule.disabled"
                await audit(connection, tenant_id=tenant, actor_type="user", actor_id=user,
                            action=action, resource_id=request.rule_id)  # fmt: skip
            return await self._rule(connection, tenant, request.rule_id)

        response: pb.SetPolicyRuleEnabledResponse = await self._change(
            "SetPolicyRuleEnabled", request, request.idempotency_key, act
        )
        return response

    # --- judging a proposal

    async def _gather(
        self, tenant: str, request: pb.EvaluateProposalRequest, now: datetime
    ) -> tuple[Any, str, list[rules.Input]]:
        """As the service itself: the case and its evidence, the turbine and its site, the estimate
        and the forecast."""
        with acting_as_service(SERVICE_NAME, tenant, self._secret):
            case = await self._sources.case(request.case_id)
            site, turbine = await sources.turbine_inputs(self._sources, case.turbine_id, now)
            found = await sources.case_inputs(self._sources, case, now)
            impact = await sources.impact_inputs(self._sources, request.impact_result_id, case)
            weather = await sources.weather_inputs(self._sources, case.turbine_id,
                                                   request.proposal)  # fmt: skip
        inputs = [*sources.proposal_inputs(request.proposal, now), *found, *turbine, *impact,
                  *weather]  # fmt: skip
        return case, site, inputs

    async def EvaluateProposal(  # noqa: N802  # The name comes from the contract.
        self, request: pb.EvaluateProposalRequest, context: Any
    ) -> pb.EvaluateProposalResponse:
        if not request.case_id:
            raise InvalidRequestError("case_id is required")
        if not request.HasField("proposal"):
            raise InvalidRequestError("proposal is required")
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        sources.check_proposal(request.proposal)
        if request.impact_result_id and not _IMPACT_ID.fullmatch(request.impact_result_id):
            raise InvalidRequestError('impact_result_id is "imp_" and 20 letters and digits')
        identity = current_identity()
        tenant, caller = identity.tenant_id, identity.user_id or identity.subject
        now = self._now()
        await self._install_defaults(tenant)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, caller, request.idempotency_key,
                                         "EvaluateProposal", request)  # fmt: skip
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            enabled = await library.enabled_rules(connection, tenant)
        if first is not None:
            return pb.EvaluateProposalResponse.FromString(first)
        try:
            case, site, inputs = await self._gather(tenant, request, now)
        except NotFoundError as error:
            raise NotFoundError("No such case") from error
        # One the caller may not read is not found, the same as one that does not exist.
        if not await self._may_read_case(case.turbine_id, site):
            raise NotFoundError("No such case")
        by_name = {item.name: item for item in inputs}
        outcomes = [rules.judge(rule, by_name, site, now) for rule, _ in enabled]
        hashes = {rule.rule_id: content for rule, content in enabled}
        result = pb.PolicyResult(
            result_id=f"pol_{uuid.uuid4().hex[:20]}", case_id=case.case_id,
            turbine_id=case.turbine_id, wind_farm_id=site, outcome=rules.worst(outcomes),
            proposal=request.proposal, inputs=[sources.to_input(i) for i in inputs],
            rules=[
                pb.RuleOutcome(
                    rule_id=o.rule.rule_id, version=o.rule.version,
                    content_hash=hashes[o.rule.rule_id], category=o.rule.category,
                    outcome=o.outcome, input=o.rule.input,
                    limit_json="" if o.limit is None else json.dumps(o.limit),
                    limit_from=o.limit_from, reason=o.reason,
                )
                for o in outcomes
            ],
            library=LIBRARY, evaluated_by=caller, evaluated_at=_timestamp(now),
        )  # fmt: skip
        content = _content(result)
        result.content_hash, result.verified = result_hash(content), True
        response = pb.EvaluateProposalResponse(result=result)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await connection.execute(
                POLICY_RESULTS.insert().values(
                    tenant_id=tenant, result_id=result.result_id, case_id=case.case_id,
                    turbine_id=case.turbine_id, outcome=result.outcome, content=content,
                    content_hash=result.content_hash, evaluated_by=caller, evaluated_at=now,
                )
            )  # fmt: skip
            await audit(
                connection, tenant_id=tenant,
                actor_type="user" if identity.user_id else "service", actor_id=caller,
                action="policy.evaluated", resource_type="policy_result",
                resource_id=result.result_id, outcome=result.outcome,
            )  # fmt: skip
            await remember(
                connection, tenant_id=tenant, user_id=caller, key=request.idempotency_key,
                method="EvaluateProposal", request=request, response=response, now=now,
            )  # fmt: skip
        return response

    async def _readable(self, row: Any) -> bool:
        if await self._authorizer.allowed(READ_CASES):
            return True
        farm = json.loads(row.content).get("wind_farm_id", "")
        return await self._authorizer.allowed(READ_CASES, farm, row.turbine_id)

    async def GetPolicyResult(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetPolicyResultRequest, context: Any
    ) -> pb.GetPolicyResultResponse:
        if not _RESULT_ID.fullmatch(request.result_id):
            raise NotFoundError("No such policy result")
        tenant = current_identity().tenant_id
        p = POLICY_RESULTS.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row = (
                await connection.execute(
                    select(POLICY_RESULTS).where(
                        p.tenant_id == tenant, p.result_id == request.result_id
                    )
                )
            ).one_or_none()
        if row is None or not await self._readable(row):
            raise NotFoundError("No such policy result")
        return pb.GetPolicyResultResponse(result=_from_row(row))

    async def ListPolicyResults(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListPolicyResultsRequest, context: Any
    ) -> pb.ListPolicyResultsResponse:
        if not request.case_id:
            raise InvalidRequestError("case_id is required")
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        offset = int(request.page_token) if request.page_token.isdigit() else 0
        tenant = current_identity().tenant_id
        p = POLICY_RESULTS.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (
                await connection.execute(
                    select(POLICY_RESULTS)
                    .where(p.tenant_id == tenant, p.case_id == request.case_id)
                    .order_by(p.evaluated_at.desc(), p.result_id)
                )
            ).all()
        if rows and not await self._readable(rows[0]):
            raise NotFoundError("No such case")
        page = rows[offset : offset + size]
        return pb.ListPolicyResultsResponse(
            results=[_from_row(row) for row in page],
            next_page_token=str(offset + size) if len(rows) > offset + size else "",
        )
