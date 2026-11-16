"""The gRPC API: versioned reference data, and deterministic impact estimates (FR-IMP-001 to 003).

Every call asks Authorization first, and stays in the caller's tenant. An estimate gathers its
facts as the service itself, checks the caller may read the case's turbine, applies the formulas
and records the result, which never changes.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from google.protobuf import json_format
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.authorization.v1 import authorization_pb2
from windops.production_impact.v1 import production_impact_pb2 as pb
from windops.production_impact.v1 import production_impact_pb2_grpc
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

from windops_production_impact import formulas, library, references, sources
from windops_production_impact.defaults import LIBRARY
from windops_production_impact.store import (
    IMPACT_RESULTS,
    REFERENCE_VERSIONS,
    REFERENCES,
    SERVICE_NAME,
    KeyReusedError,
    audit,
    remember,
    remembered,
)

READ, MANAGE = "policy.read", "policy.manage"
# An estimate prices a case's proposal, so those who may read the case may ask for one.
READ_CASES = "investigation.read"
RESULT_FORMAT = "windops-impact-result-v1"
WINDOW = timedelta(hours=6)
# Samples reach storage within seconds, so the window ends a little before now.
SETTLE = timedelta(seconds=30)
MAXIMUM_DOWNTIME_HOURS = 720.0
DEFAULT_PAGE, MAXIMUM_PAGE = 50, 200
MAXIMUM_KEY, MAXIMUM_TEXT, MAXIMUM_DEFINITION = 64, 500, 20000
_REFERENCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
_RESULT_ID = re.compile(r"imp_[a-z0-9]{20}")


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
    return sources.stamp(value if value.tzinfo else value.replace(tzinfo=UTC))


def _version(row: Any) -> pb.ReferenceVersion:
    return pb.ReferenceVersion(
        reference_id=row.reference_id, version=row.version, kind=row.kind,
        definition_json=row.definition, content_hash=row.content_hash, created_by=row.created_by,
        created_at=_timestamp(row.created_at), reason=row.reason,
    )  # fmt: skip


def _content(result: pb.ImpactResult) -> str:
    plain = pb.ImpactResult()
    plain.CopyFrom(result)
    plain.content_hash, plain.verified = "", False
    data = json_format.MessageToDict(plain, preserving_proto_field_name=True)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def result_hash(content: str) -> str:
    return hashlib.sha256(f"{RESULT_FORMAT}|{content}".encode()).hexdigest()


def _from_row(row: Any) -> pb.ImpactResult:
    result = json_format.ParseDict(json.loads(row.content), pb.ImpactResult())
    result.content_hash = row.content_hash
    # Read back, the result is hashed again: a changed row no longer verifies.
    result.verified = result_hash(row.content) == row.content_hash
    return result


def _input(name: str, value: Any, source: str, ref: str = "",
           at: datetime | None = None) -> pb.ImpactInput:  # fmt: skip
    return pb.ImpactInput(name=name, value_json=json.dumps(value), source=source,
                          source_ref=ref, observed_at=_timestamp(at))  # fmt: skip


def _curve_for(found: dict[str, library.Current], manufacturer: str, model: str) -> Any:
    for item in sorted(found.values(), key=lambda c: c.reference_id):
        curve = item.parsed
        if item.kind == "power_curve" and (curve.manufacturer, curve.model) == (manufacturer,
                                                                                model):  # fmt: skip
            return item
    raise ConflictError(
        f"There is no power curve for the {manufacturer} {model}. A Tenant Administrator can "
        "publish one"
    )


class ProductionImpactService(production_impact_pb2_grpc.ProductionImpactServiceServicer):
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
            raise PermissionDeniedError("Only a person can change reference data")
        if not await self._authorizer.allowed(MANAGE):
            raise PermissionDeniedError("You do not have permission to do this")
        try:
            require_recent_mfa(authentication_claims(identity))
        except MfaRequiredError as error:
            raise PermissionDeniedError(STEP_UP_MESSAGE) from error
        return identity.tenant_id, identity.user_id

    async def _may_read_turbine(self, turbine_id: str, wind_farm_id: str) -> bool:
        if await self._authorizer.allowed(READ_CASES):
            return True
        return bool(turbine_id) and await self._authorizer.allowed(
            READ_CASES, wind_farm_id, turbine_id
        )

    # --- the reference data

    async def _references(self, connection: AsyncConnection, tenant: str) -> list[pb.Reference]:
        r, v = REFERENCES.c, REFERENCE_VERSIONS.c
        rows = (
            await connection.execute(
                select(REFERENCES, REFERENCE_VERSIONS)
                .join(
                    REFERENCE_VERSIONS,
                    (v.tenant_id == r.tenant_id)
                    & (v.reference_id == r.reference_id)
                    & (v.version == r.current_version),
                )
                .where(r.tenant_id == tenant)
                .order_by(r.reference_id)
            )
        ).all()
        return [pb.Reference(reference_id=row.reference_id, name=row.name, current=_version(row))
                for row in rows]  # fmt: skip

    async def _reference(self, connection: AsyncConnection, tenant: str, ref: str) -> Any:
        found = [r for r in await self._references(connection, tenant) if r.reference_id == ref]
        if not found:
            raise NotFoundError("No such reference")
        return found[0]

    async def ListReferences(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListReferencesRequest, context: Any
    ) -> pb.ListReferencesResponse:
        tenant = await self._require_read()
        await library.install_defaults(self._engine, tenant, self._now())
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            return pb.ListReferencesResponse(
                references=await self._references(connection, tenant), library=LIBRARY
            )

    async def GetReference(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetReferenceRequest, context: Any
    ) -> pb.GetReferenceResponse:
        tenant = await self._require_read()
        await library.install_defaults(self._engine, tenant, self._now())
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            reference = await self._reference(connection, tenant, request.reference_id)
            v = REFERENCE_VERSIONS.c
            rows = (
                await connection.execute(
                    select(REFERENCE_VERSIONS)
                    .where(v.tenant_id == tenant, v.reference_id == request.reference_id)
                    .order_by(v.version)
                )
            ).all()
        return pb.GetReferenceResponse(reference=reference, versions=[_version(r) for r in rows])

    async def PublishReferenceVersion(  # noqa: N802  # The name comes from the contract.
        self, request: pb.PublishReferenceVersionRequest, context: Any
    ) -> pb.PublishReferenceVersionResponse:
        if not _REFERENCE_ID.fullmatch(request.reference_id):
            raise InvalidRequestError(
                "reference_id is 3 to 64 lowercase letters, digits and hyphens"
            )
        reason, name = request.reason.strip(), request.name.strip()
        if not reason or len(reason) > MAXIMUM_TEXT or len(name) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"reason is required; reason and name are at most "
                                      f"{MAXIMUM_TEXT} characters")  # fmt: skip
        if len(request.definition_json) > MAXIMUM_DEFINITION:
            raise InvalidRequestError(f"definition_json is at most {MAXIMUM_DEFINITION} characters")
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        try:
            definition = json.loads(request.definition_json)
            if not isinstance(definition, dict):
                raise InvalidRequestError("definition_json must be an object")
            references.parse(definition)
        except ValueError as error:
            raise InvalidRequestError(f"The reference cannot be used: {error}") from error
        tenant, user = await self._require_manage()
        now = self._now()
        await library.install_defaults(self._engine, tenant, now)
        method = "PublishReferenceVersion"
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                try:
                    first = await remembered(connection, tenant, user, request.idempotency_key,
                                             method, request)  # fmt: skip
                except KeyReusedError as error:
                    raise InvalidRequestError(str(error)) from error
                if first is not None:
                    return pb.PublishReferenceVersionResponse.FromString(first)
                r = REFERENCES.c
                row = (
                    await connection.execute(
                        select(REFERENCES).where(
                            r.tenant_id == tenant, r.reference_id == request.reference_id
                        )
                    )
                ).one_or_none()
                have = row.current_version if row is not None else 0
                if have != request.expected_version:
                    raise ConflictError(
                        f"This reference is at version {have}, not {request.expected_version}. "
                        "Read it, and try again"
                    )
                if row is not None and row.kind != definition["kind"]:
                    raise InvalidRequestError(f"This reference is a {row.kind}, not a "
                                              f"{definition['kind']}")  # fmt: skip
                await library.write_version(connection, tenant, request.reference_id, have + 1,
                                            definition, user, reason, now)  # fmt: skip
                if row is None:
                    if not name:
                        raise InvalidRequestError("A new reference needs a name")
                    await connection.execute(REFERENCES.insert().values(
                        tenant_id=tenant, reference_id=request.reference_id, name=name,
                        kind=definition["kind"], current_version=1, created_at=now,
                        updated_at=now,
                    ))  # fmt: skip
                    action = "impact_reference.created"
                else:
                    await connection.execute(
                        update(REFERENCES)
                        .where(r.tenant_id == tenant, r.reference_id == request.reference_id)
                        .values(current_version=have + 1, updated_at=now, name=name or row.name)
                    )
                    action = "impact_reference.version_published"
                await audit(connection, tenant_id=tenant, actor_type="user", actor_id=user,
                            action=action, resource_id=request.reference_id)  # fmt: skip
                response = pb.PublishReferenceVersionResponse(
                    reference=await self._reference(connection, tenant, request.reference_id)
                )
                await remember(connection, tenant_id=tenant, user_id=user,
                               key=request.idempotency_key, method=method, request=request,
                               response=response, now=now)  # fmt: skip
        except IntegrityError as error:
            raise ConflictError(
                "Someone else changed this reference first. Read it, and try again"
            ) from error
        return response

    # --- estimates

    async def EstimateImpact(  # noqa: N802  # The name comes from the contract.
        self, request: pb.EstimateImpactRequest, context: Any
    ) -> pb.EstimateImpactResponse:
        if not request.case_id:
            raise InvalidRequestError("case_id is required")
        if request.action not in references.ACTIONS:
            raise InvalidRequestError(f"action is one of {', '.join(references.ACTIONS)}")
        if not 0 < request.downtime_hours <= MAXIMUM_DOWNTIME_HOURS:
            raise InvalidRequestError(
                f"downtime_hours is more than 0, and at most {MAXIMUM_DOWNTIME_HOURS:g}"
            )
        if len(request.idempotency_key) > MAXIMUM_KEY:
            raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
        identity = current_identity()
        tenant, caller = identity.tenant_id, identity.user_id or identity.subject
        now = self._now()
        await library.install_defaults(self._engine, tenant, now)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, caller, request.idempotency_key,
                                         "EstimateImpact", request)  # fmt: skip
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            found = await library.current(connection, tenant)
        if first is not None:
            return pb.EstimateImpactResponse.FromString(first)
        end = now - SETTLE
        start = end - WINDOW
        with acting_as_service(SERVICE_NAME, tenant, self._secret):
            try:
                case = await self._sources.case(request.case_id)
            except NotFoundError as error:
                raise NotFoundError("No such case") from error
            turbine = await self._sources.turbine(case.turbine_id)
            farm = await self._sources.wind_farm(turbine.wind_farm_id)
            samples = await self._sources.samples(case.turbine_id, start, end)
        # One the caller may not read is not found, the same as one that does not exist.
        if not await self._may_read_turbine(case.turbine_id, turbine.wind_farm_id):
            raise NotFoundError("No such case")
        curve = _curve_for(found, turbine.model.manufacturer, turbine.model.model)
        price, costs = found["energy-price"], found["maintenance-costs"]
        try:
            figures = formulas.estimate(
                samples, curve.parsed, price.parsed, costs.parsed, action=request.action,
                downtime_hours=request.downtime_hours, offshore=farm.offshore,
                site=turbine.wind_farm_id,
            )  # fmt: skip
        except formulas.NotEnoughDataError as error:
            raise ConflictError(f"No estimate: {error}") from error
        turbine_ref = f"{turbine.turbine_id}@{turbine.version}"
        result = pb.ImpactResult(
            result_id=f"imp_{uuid.uuid4().hex[:20]}", case_id=case.case_id,
            turbine_id=case.turbine_id, wind_farm_id=turbine.wind_farm_id,
            action=request.action, downtime_hours=request.downtime_hours,
            formula_version=formulas.FORMULA_VERSION,
            inputs=[
                _input("request.action", request.action, "request"),
                _input("request.downtime_hours", request.downtime_hours, "request"),
                _input("turbine.model", f"{turbine.model.manufacturer} {turbine.model.model}",
                       "asset", turbine_ref, now),
                _input("site.offshore", farm.offshore, "asset",
                       f"{farm.wind_farm_id}@{farm.version}", now),
                _input("observed.mean_wind_mps", round(figures.mean_wind_mps, 3), "telemetry",
                       case.turbine_id, end),
                _input("observed.seconds", round(figures.observed_seconds), "telemetry",
                       case.turbine_id, end),
                *[
                    _input(f"reference.{item.reference_id}",
                           {"version": item.version, "content_hash": item.content_hash},
                           "reference", f"{item.reference_id}@{item.version}")
                    for item in (curve, price, costs)
                ],
            ],
            figures=[pb.Figure(name=f.name, value=round(f.value, 4), low=round(f.low, 4),
                               high=round(f.high, 4), unit=f.unit) for f in figures.figures],
            assumptions=figures.assumptions, window_start=_timestamp(start),
            window_end=_timestamp(end), estimated_by=caller, estimated_at=_timestamp(now),
        )  # fmt: skip
        content = _content(result)
        result.content_hash, result.verified = result_hash(content), True
        response = pb.EstimateImpactResponse(result=result)
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            await connection.execute(IMPACT_RESULTS.insert().values(
                tenant_id=tenant, result_id=result.result_id, case_id=case.case_id,
                turbine_id=case.turbine_id, content=content, content_hash=result.content_hash,
                estimated_by=caller, estimated_at=now,
            ))  # fmt: skip
            await audit(connection, tenant_id=tenant,
                        actor_type="user" if identity.user_id else "service", actor_id=caller,
                        action="impact.estimated", resource_type="impact_result",
                        resource_id=result.result_id)  # fmt: skip
            await remember(connection, tenant_id=tenant, user_id=caller,
                           key=request.idempotency_key, method="EstimateImpact", request=request,
                           response=response, now=now)  # fmt: skip
        return response

    async def _readable(self, row: Any) -> bool:
        if await self._authorizer.allowed(READ_CASES):
            return True
        farm = json.loads(row.content).get("wind_farm_id", "")
        return await self._authorizer.allowed(READ_CASES, farm, row.turbine_id)

    async def GetImpactResult(  # noqa: N802  # The name comes from the contract.
        self, request: pb.GetImpactResultRequest, context: Any
    ) -> pb.GetImpactResultResponse:
        if not _RESULT_ID.fullmatch(request.result_id):
            raise NotFoundError("No such impact result")
        tenant = current_identity().tenant_id
        i = IMPACT_RESULTS.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row = (
                await connection.execute(
                    select(IMPACT_RESULTS).where(
                        i.tenant_id == tenant, i.result_id == request.result_id
                    )
                )
            ).one_or_none()
        if row is None or not await self._readable(row):
            raise NotFoundError("No such impact result")
        return pb.GetImpactResultResponse(result=_from_row(row))

    async def ListImpactResults(  # noqa: N802  # The name comes from the contract.
        self, request: pb.ListImpactResultsRequest, context: Any
    ) -> pb.ListImpactResultsResponse:
        if not request.case_id:
            raise InvalidRequestError("case_id is required")
        size = request.page_size or DEFAULT_PAGE
        if not 1 <= size <= MAXIMUM_PAGE:
            raise InvalidRequestError(f"page_size is from 1 to {MAXIMUM_PAGE}")
        offset = int(request.page_token) if request.page_token.isdigit() else 0
        tenant = current_identity().tenant_id
        i = IMPACT_RESULTS.c
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (
                await connection.execute(
                    select(IMPACT_RESULTS)
                    .where(i.tenant_id == tenant, i.case_id == request.case_id)
                    .order_by(i.estimated_at.desc(), i.result_id)
                )
            ).all()
        if rows and not await self._readable(rows[0]):
            raise NotFoundError("No such case")
        page = rows[offset : offset + size]
        return pb.ListImpactResultsResponse(
            results=[_from_row(row) for row in page],
            next_page_token=str(offset + size) if len(rows) > offset + size else "",
        )
