"""The gRPC API: datasets, jobs and their attempts, benchmarks and comparisons.

Every call needs evaluation.manage, held only by a Platform Administrator, and no agent ever
calls it: the ground truth here never reaches an agent (SIM-005). The rules themselves are in
rules.py; this keeps them, in the database, under the caller's tenant.
"""

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.evaluation.v1 import evaluation_pb2 as pb
from windops.evaluation.v1 import evaluation_pb2_grpc
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    current_identity,
)
from windops_migrations import in_tenant

from windops_evaluation import rules
from windops_evaluation.store import (
    ATTEMPTS,
    BENCHMARKS,
    CANCELLED,
    DATASETS,
    FINISHED,
    JOB_STATUSES,
    JOBS,
    PASSED,
    PLANNED,
    QUEUED,
    RUNNING,
    SCENARIOS,
    KeyReusedError,
    audit,
    remember,
    remembered,
)

MANAGE = "evaluation.manage"
DEFAULT_PAGE, MAXIMUM_PAGE, MAXIMUM_KEY = 50, 200, 64
MAXIMUM_SCENARIOS, MAXIMUM_DEFINITION, MAXIMUM_TEXT = 500, 64_000, 1000
DEFAULT_LEASE, MAXIMUM_LEASE = 600, 3600
MAXIMUM_VERSIONS = 40


class Authorizer(Protocol):
    async def allowed(self, permission: str) -> bool: ...


def _timestamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _page(size: int) -> int:
    if size < 0:
        raise InvalidRequestError("page_size cannot be negative")
    return min(size or DEFAULT_PAGE, MAXIMUM_PAGE)


def _offset(token: str) -> int:
    if not token:
        return 0
    if not token.isdigit():
        raise InvalidRequestError("page_token is not one this service gave")
    return int(token)


def _key(value: str) -> str:
    if len(value) > MAXIMUM_KEY:
        raise InvalidRequestError(f"idempotency_key is at most {MAXIMUM_KEY} characters")
    return value


def _scenario(row: Any, with_definition: bool = True) -> pb.Scenario:
    return pb.Scenario(
        scenario_id=row.scenario_id, family=row.family, split=row.split, severity=row.severity,
        runs_required=row.runs_required,
        definition_json=row.definition_json if with_definition else "",
    )  # fmt: skip


def _dataset(row: Any, scenarios: list[Any] | None = None) -> pb.Dataset:
    return pb.Dataset(
        name=row.name, version=row.version, description=row.description,
        content_sha256=row.content_sha256, scenario_count=row.scenario_count,
        registered_by=row.registered_by, registered_at=_timestamp(row.registered_at),
        scenarios=[_scenario(s) for s in scenarios or []],
    )  # fmt: skip


def _attempt(row: Any) -> pb.Attempt:
    scores = pb.ReportAttemptRequest.FromString(row.scores).attempt.scores if row.scores else []
    return pb.Attempt(
        scenario_id=row.scenario_id, attempt=row.attempt, status=row.status, run_id=row.run_id,
        case_id=row.case_id, rehearsed=row.rehearsed, scores=scores, seconds=row.seconds,
        tokens=row.tokens, cost_usd=row.cost_usd, reason=row.reason,
        reported_at=_timestamp(row.reported_at),
    )  # fmt: skip


def _evaluation(row: Any, attempts: list[Any], with_attempts: bool) -> pb.Evaluation:
    counted = rules.totals([a.status for a in attempts])
    return pb.Evaluation(
        job_id=row.job_id, dataset_name=row.dataset_name, dataset_version=row.dataset_version,
        splits=[s for s in row.splits.split(",") if s], mode=row.mode, status=row.status,
        requested_by=row.requested_by, requested_at=_timestamp(row.requested_at),
        runner=row.runner, lease_expires_at=_timestamp(row.lease_expires_at),
        started_at=_timestamp(row.started_at), finished_at=_timestamp(row.finished_at),
        versions=json.loads(row.versions_json), planned=counted.planned, passed=counted.passed,
        failed=counted.failed, incomplete=counted.incomplete,
        cost_usd=sum(a.cost_usd for a in attempts), tokens=sum(a.tokens for a in attempts),
        seconds=sum(a.seconds for a in attempts), note=row.note,
        attempts=[_attempt(a) for a in attempts] if with_attempts else [],
    )  # fmt: skip


class EvaluationService(evaluation_pb2_grpc.EvaluationServiceServicer):
    def __init__(
        self,
        engine: AsyncEngine,
        authorizer: Authorizer,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine, self._authorizer = engine, authorizer
        self._now = clock or (lambda: datetime.now(UTC))

    async def _caller(self) -> tuple[Any, str, str]:
        """The caller, their tenant and who they are, once they may manage evaluation. No agent
        ever may, whatever it was granted: the ground truth here never reaches one (SIM-005)."""
        identity = current_identity()
        if identity.actor:
            async with self._engine.begin() as connection:
                await in_tenant(connection, identity.tenant_id)
                await audit(connection, identity, action="authorization.denied",
                            resource_type="permission", resource_id=MANAGE,
                            outcome="denied")  # fmt: skip
            raise PermissionDeniedError("No agent may use the Evaluation Service (SIM-005)")
        if not await self._authorizer.allowed(MANAGE):
            raise PermissionDeniedError("Only a Platform Administrator manages evaluation")
        return identity, identity.tenant_id, str(identity.user_id or identity.subject)

    # --- storage

    @staticmethod
    async def _dataset_rows(
        connection: AsyncConnection, tenant: str, name: str, version: str
    ) -> tuple[Any, list[Any]]:
        d, s = DATASETS.c, SCENARIOS.c
        row = (await connection.execute(select(DATASETS).where(
            d.tenant_id == tenant, d.name == name, d.version == version,
        ))).one_or_none()  # fmt: skip
        if row is None:
            raise NotFoundError(f"No dataset {name} version {version}")
        scenarios = (await connection.execute(
            select(SCENARIOS).where(s.tenant_id == tenant, s.dataset_name == name,
                                    s.dataset_version == version).order_by(s.scenario_id)
        )).all()  # fmt: skip
        return row, list(scenarios)

    @staticmethod
    async def _job(
        connection: AsyncConnection, tenant: str, job_id: str, lock: bool = False
    ) -> Any:
        j = JOBS.c
        query = select(JOBS).where(j.tenant_id == tenant, j.job_id == job_id)
        row = (await connection.execute(query.with_for_update() if lock else query)).one_or_none()
        if row is None:
            raise NotFoundError("No such evaluation")
        return row

    @staticmethod
    async def _attempts(connection: AsyncConnection, tenant: str, job_id: str) -> list[Any]:
        a = ATTEMPTS.c
        return list((await connection.execute(
            select(ATTEMPTS).where(a.tenant_id == tenant, a.job_id == job_id)
            .order_by(a.scenario_id, a.attempt)
        )).all())  # fmt: skip

    async def _message(
        self, connection: AsyncConnection, tenant: str, job_id: str, with_attempts: bool = True
    ) -> pb.Evaluation:
        row = await self._job(connection, tenant, job_id)
        return _evaluation(row, await self._attempts(connection, tenant, job_id), with_attempts)

    def _leased(self, row: Any, token: str) -> None:
        """Only the runner holding the lease reports, renews or finishes."""
        if row.status != RUNNING:
            raise ConflictError(f"This evaluation is {row.status}, not running")
        if not token or not secrets.compare_digest(token, row.lease_token):
            raise ConflictError("This lease is not yours, or has been taken over: lease it again")
        if row.lease_expires_at is None or _aware(row.lease_expires_at) <= self._now():
            raise ConflictError("The lease ran out: lease the evaluation again")

    @staticmethod
    def _lease_seconds(value: int) -> int:
        if value < 0 or value > MAXIMUM_LEASE:
            raise InvalidRequestError(f"lease_seconds is from 1 to {MAXIMUM_LEASE}")
        return value or DEFAULT_LEASE

    # --- datasets

    async def RegisterDataset(  # noqa: N802  # The name comes from the contract.
        self, request: pb.RegisterDatasetRequest, context: Any
    ) -> pb.RegisterDatasetResponse:
        identity, tenant, who = await self._caller()
        key = _key(request.idempotency_key)
        if len(request.scenarios) > MAXIMUM_SCENARIOS:
            raise InvalidRequestError(f"A dataset holds at most {MAXIMUM_SCENARIOS} scenarios")
        if len(request.description) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"A description is at most {MAXIMUM_TEXT} characters")
        if any(len(s.definition_json) > MAXIMUM_DEFINITION for s in request.scenarios):
            raise InvalidRequestError(f"A definition is at most {MAXIMUM_DEFINITION} characters")
        specs = [
            rules.ScenarioSpec(s.scenario_id, s.family, s.split, s.severity, s.runs_required,
                               s.definition_json)
            for s in request.scenarios
        ]  # fmt: skip
        try:
            rules.check_dataset(request.name, request.version, specs)
        except rules.RuleError as error:
            raise InvalidRequestError(str(error)) from error
        digest = rules.content_hash(specs)
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, who, key, "RegisterDataset", request)
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.RegisterDatasetResponse.FromString(first)
            d = DATASETS.c
            existing = (await connection.execute(select(DATASETS).where(
                d.tenant_id == tenant, d.name == request.name, d.version == request.version,
            ).with_for_update())).one_or_none()  # fmt: skip
            if existing is not None:
                if existing.content_sha256 != digest:
                    raise ConflictError(
                        f"{request.name} version {request.version} is registered with other "
                        "content, and a version never changes: give the new one a new version"
                    )
                row, scenarios = await self._dataset_rows(connection, tenant, request.name,
                                                          request.version)  # fmt: skip
                return pb.RegisterDatasetResponse(dataset=_dataset(row, scenarios), created=False)
            await connection.execute(insert(DATASETS).values(
                tenant_id=tenant, name=request.name, version=request.version,
                description=request.description, content_sha256=digest,
                scenario_count=len(specs), registered_by=who, registered_at=now,
            ))  # fmt: skip
            await connection.execute(insert(SCENARIOS), [
                {"tenant_id": tenant, "dataset_name": request.name,
                 "dataset_version": request.version, "scenario_id": s.scenario_id,
                 "family": s.family, "split": s.split, "severity": s.severity,
                 "runs_required": s.runs_required, "definition_json": s.definition_json}
                for s in specs
            ])  # fmt: skip
            await audit(connection, identity, action="evaluation.dataset.registered",
                        resource_type="evaluation_dataset",
                        resource_id=f"{request.name}@{request.version}")  # fmt: skip
            row, scenarios = await self._dataset_rows(connection, tenant, request.name,
                                                      request.version)  # fmt: skip
            response = pb.RegisterDatasetResponse(dataset=_dataset(row, scenarios), created=True)
            await remember(connection, tenant_id=tenant, caller=who, key=key,
                           method="RegisterDataset", request=request, response=response,
                           now=now)  # fmt: skip
        return response

    async def GetDataset(  # noqa: N802
        self, request: pb.GetDatasetRequest, context: Any
    ) -> pb.GetDatasetResponse:
        _, tenant, _ = await self._caller()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            row, scenarios = await self._dataset_rows(connection, tenant, request.name,
                                                      request.version)  # fmt: skip
        return pb.GetDatasetResponse(dataset=_dataset(row, scenarios))

    async def ListDatasets(  # noqa: N802
        self, request: pb.ListDatasetsRequest, context: Any
    ) -> pb.ListDatasetsResponse:
        _, tenant, _ = await self._caller()
        size, offset = _page(request.page_size), _offset(request.page_token)
        d = DATASETS.c
        query = select(DATASETS).where(d.tenant_id == tenant)
        if request.name:
            query = query.where(d.name == request.name)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(
                query.order_by(d.registered_at.desc(), d.name, d.version)
                .offset(offset).limit(size + 1)
            )).all()  # fmt: skip
        more = len(rows) > size
        return pb.ListDatasetsResponse(
            datasets=[_dataset(r) for r in rows[:size]],
            next_page_token=str(offset + size) if more else "",
        )

    # --- jobs

    async def StartEvaluation(  # noqa: N802
        self, request: pb.StartEvaluationRequest, context: Any
    ) -> pb.StartEvaluationResponse:
        identity, tenant, who = await self._caller()
        key = _key(request.idempotency_key)
        if len(request.note) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"A note is at most {MAXIMUM_TEXT} characters")
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, who, key, "StartEvaluation", request)
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.StartEvaluationResponse.FromString(first)
            _, scenarios = await self._dataset_rows(connection, tenant, request.dataset_name,
                                                    request.dataset_version)  # fmt: skip
            specs = [rules.ScenarioSpec(s.scenario_id, s.family, s.split, s.severity,
                                        s.runs_required, "") for s in scenarios]  # fmt: skip
            try:
                planned = rules.plan(request.mode, specs, list(request.splits),
                                     list(request.scenario_ids))  # fmt: skip
            except rules.RuleError as error:
                raise InvalidRequestError(str(error)) from error
            chosen = {s.scenario_id: s.split for s in specs}
            splits = rules.splits_key(request.splits or {chosen[i] for i, _ in planned})
            job_id = f"evj_{secrets.token_hex(10)}"
            await connection.execute(insert(JOBS).values(
                tenant_id=tenant, job_id=job_id, dataset_name=request.dataset_name,
                dataset_version=request.dataset_version, splits=splits, mode=request.mode,
                status=QUEUED, requested_by=who, requested_at=now, note=request.note.strip(),
            ))  # fmt: skip
            await connection.execute(insert(ATTEMPTS), [
                {"tenant_id": tenant, "job_id": job_id, "scenario_id": scenario_id,
                 "attempt": attempt, "status": PLANNED}
                for scenario_id, attempt in planned
            ])  # fmt: skip
            await audit(connection, identity, action="evaluation.started",
                        resource_type="evaluation", resource_id=job_id)  # fmt: skip
            response = pb.StartEvaluationResponse(
                evaluation=await self._message(connection, tenant, job_id)
            )
            await remember(connection, tenant_id=tenant, caller=who, key=key,
                           method="StartEvaluation", request=request, response=response,
                           now=now)  # fmt: skip
        return response

    async def GetEvaluation(  # noqa: N802
        self, request: pb.GetEvaluationRequest, context: Any
    ) -> pb.GetEvaluationResponse:
        _, tenant, _ = await self._caller()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            return pb.GetEvaluationResponse(
                evaluation=await self._message(connection, tenant, request.job_id)
            )

    async def ListEvaluations(  # noqa: N802
        self, request: pb.ListEvaluationsRequest, context: Any
    ) -> pb.ListEvaluationsResponse:
        _, tenant, _ = await self._caller()
        if request.status and request.status not in JOB_STATUSES:
            raise InvalidRequestError(f"status is one of {', '.join(JOB_STATUSES)}")
        size, offset = _page(request.page_size), _offset(request.page_token)
        j = JOBS.c
        query = select(JOBS).where(j.tenant_id == tenant)
        if request.dataset_name:
            query = query.where(j.dataset_name == request.dataset_name)
        if request.status:
            query = query.where(j.status == request.status)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(
                query.order_by(j.requested_at.desc(), j.job_id).offset(offset).limit(size + 1)
            )).all()  # fmt: skip
            found = [
                _evaluation(r, await self._attempts(connection, tenant, r.job_id), False)
                for r in rows[:size]
            ]
        return pb.ListEvaluationsResponse(
            evaluations=found, next_page_token=str(offset + size) if len(rows) > size else ""
        )

    async def LeaseEvaluation(  # noqa: N802
        self, request: pb.LeaseEvaluationRequest, context: Any
    ) -> pb.LeaseEvaluationResponse:
        identity, tenant, _ = await self._caller()
        runner = request.runner.strip()
        if not runner or len(runner) > 200:
            raise InvalidRequestError("Name the runner, in at most 200 characters")
        seconds = self._lease_seconds(request.lease_seconds)
        now = self._now()
        j = JOBS.c
        # Waiting, or running with a lease that ran out: a runner that stopped is taken over.
        free = (j.status == QUEUED) | ((j.status == RUNNING) & (j.lease_expires_at <= now))
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            query = select(JOBS).where(j.tenant_id == tenant)
            if request.job_id:
                row = await self._job(connection, tenant, request.job_id, lock=True)
                if not (row.status == QUEUED or (
                    row.status == RUNNING and row.lease_expires_at is not None
                    and _aware(row.lease_expires_at) <= now
                )):  # fmt: skip
                    raise ConflictError(f"This evaluation is {row.status}, and leased or done")
            else:
                row = (await connection.execute(
                    query.where(free).order_by(j.requested_at, j.job_id).limit(1)
                    .with_for_update(skip_locked=True)
                )).one_or_none()  # fmt: skip
                if row is None:
                    return pb.LeaseEvaluationResponse()
            token = secrets.token_urlsafe(24)
            await connection.execute(
                update(JOBS).where(j.tenant_id == tenant, j.job_id == row.job_id).values(
                    status=RUNNING, runner=runner, lease_token=token,
                    lease_expires_at=now + timedelta(seconds=seconds),
                    started_at=row.started_at or now,
                )
            )  # fmt: skip
            await audit(connection, identity, action="evaluation.leased",
                        resource_type="evaluation", resource_id=row.job_id)  # fmt: skip
            message = await self._message(connection, tenant, row.job_id)
        return pb.LeaseEvaluationResponse(evaluation=message, lease_token=token)

    async def RenewLease(  # noqa: N802
        self, request: pb.RenewLeaseRequest, context: Any
    ) -> pb.RenewLeaseResponse:
        _, tenant, _ = await self._caller()
        seconds = self._lease_seconds(request.lease_seconds)
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._job(connection, tenant, request.job_id, lock=True)
            self._leased(row, request.lease_token)
            until = now + timedelta(seconds=seconds)
            j = JOBS.c
            await connection.execute(
                update(JOBS)
                .where(j.tenant_id == tenant, j.job_id == row.job_id)
                .values(lease_expires_at=until)
            )
        return pb.RenewLeaseResponse(lease_expires_at=_timestamp(until))

    async def ReportAttempt(  # noqa: N802
        self, request: pb.ReportAttemptRequest, context: Any
    ) -> pb.ReportAttemptResponse:
        _, tenant, _ = await self._caller()
        sent = request.attempt
        if len(sent.reason) > MAXIMUM_TEXT or len(request.versions) > MAXIMUM_VERSIONS:
            raise InvalidRequestError("The reason or versions are too long")
        if sent.seconds < 0 or sent.tokens < 0 or sent.cost_usd < 0:
            raise InvalidRequestError("Time, tokens and cost cannot be negative")
        if len(sent.run_id) > 100 or len(sent.case_id) > 100:
            raise InvalidRequestError("run_id and case_id are at most 100 characters")
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._job(connection, tenant, request.job_id, lock=True)
            self._leased(row, request.lease_token)
            try:
                reported = rules.attempt_result(
                    row.mode, sent.status, sent.rehearsed,
                    [(s.key, s.result) for s in sent.scores], sent.reason,
                )  # fmt: skip
            except rules.RuleError as error:
                raise InvalidRequestError(str(error)) from error
            a = ATTEMPTS.c
            where = (a.tenant_id == tenant, a.job_id == row.job_id,
                     a.scenario_id == sent.scenario_id, a.attempt == sent.attempt)  # fmt: skip
            planned = (await connection.execute(select(ATTEMPTS).where(*where))).one_or_none()
            if planned is None:
                raise NotFoundError("That attempt is not part of this evaluation's plan")
            kept = pb.ReportAttemptRequest(attempt=pb.Attempt(scores=sent.scores))
            values = {
                "status": reported.status, "run_id": sent.run_id, "case_id": sent.case_id,
                "rehearsed": sent.rehearsed, "scores": kept.SerializeToString(),
                "seconds": sent.seconds, "tokens": sent.tokens, "cost_usd": sent.cost_usd,
                "reason": reported.reason,
            }  # fmt: skip
            if planned.status != PLANNED:
                same = all(getattr(planned, k) == v for k, v in values.items())
                if not same:
                    raise ConflictError("That attempt was already reported, with another result")
                return pb.ReportAttemptResponse(attempt=_attempt(planned))
            await connection.execute(
                update(ATTEMPTS).where(*where).values(**values, reported_at=now)
            )
            if request.versions:
                merged = {**json.loads(row.versions_json), **dict(request.versions)}
                j = JOBS.c
                await connection.execute(update(JOBS).where(
                    j.tenant_id == tenant, j.job_id == row.job_id,
                ).values(versions_json=json.dumps(merged, sort_keys=True)))  # fmt: skip
            done = (await connection.execute(select(ATTEMPTS).where(*where))).one()
        return pb.ReportAttemptResponse(attempt=_attempt(done))

    async def FinishEvaluation(  # noqa: N802
        self, request: pb.FinishEvaluationRequest, context: Any
    ) -> pb.FinishEvaluationResponse:
        identity, tenant, _ = await self._caller()
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._job(connection, tenant, request.job_id, lock=True)
            self._leased(row, request.lease_token)
            attempts = await self._attempts(connection, tenant, row.job_id)
            status = rules.outcome([a.status for a in attempts])
            j = JOBS.c
            await connection.execute(update(JOBS).where(
                j.tenant_id == tenant, j.job_id == row.job_id,
            ).values(status=status, finished_at=now, lease_token="",
                     lease_expires_at=None))  # fmt: skip
            await audit(connection, identity, action=f"evaluation.{status}",
                        resource_type="evaluation", resource_id=row.job_id,
                        outcome="succeeded" if status == PASSED else "failed")  # fmt: skip
            message = await self._message(connection, tenant, row.job_id)
        return pb.FinishEvaluationResponse(evaluation=message)

    async def CancelEvaluation(  # noqa: N802
        self, request: pb.CancelEvaluationRequest, context: Any
    ) -> pb.CancelEvaluationResponse:
        identity, tenant, who = await self._caller()
        key = _key(request.idempotency_key)
        reason = request.reason.strip()
        if not reason or len(reason) > MAXIMUM_TEXT:
            raise InvalidRequestError(f"Give a reason, of at most {MAXIMUM_TEXT} characters")
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, who, key, "CancelEvaluation", request)
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.CancelEvaluationResponse.FromString(first)
            row = await self._job(connection, tenant, request.job_id, lock=True)
            if row.status in FINISHED:
                raise ConflictError(f"This evaluation is already {row.status}")
            note = f"{row.note}\nCancelled: {reason}".strip()
            j = JOBS.c
            await connection.execute(update(JOBS).where(
                j.tenant_id == tenant, j.job_id == row.job_id,
            ).values(status=CANCELLED, finished_at=now, lease_token="", lease_expires_at=None,
                     note=note))  # fmt: skip
            await audit(connection, identity, action="evaluation.cancelled",
                        resource_type="evaluation", resource_id=row.job_id)  # fmt: skip
            response = pb.CancelEvaluationResponse(
                evaluation=await self._message(connection, tenant, row.job_id)
            )
            await remember(connection, tenant_id=tenant, caller=who, key=key,
                           method="CancelEvaluation", request=request, response=response,
                           now=now)  # fmt: skip
        return response

    # --- benchmarks

    @staticmethod
    def _benchmark(row: Any, dataset_version: str) -> pb.Benchmark:
        return pb.Benchmark(
            dataset_name=row.dataset_name, splits=[s for s in row.splits.split(",") if s],
            mode=row.mode, job_id=row.job_id, dataset_version=dataset_version,
            set_by=row.set_by, set_at=_timestamp(row.set_at),
        )  # fmt: skip

    async def SetBenchmark(  # noqa: N802
        self, request: pb.SetBenchmarkRequest, context: Any
    ) -> pb.SetBenchmarkResponse:
        identity, tenant, who = await self._caller()
        key = _key(request.idempotency_key)
        now = self._now()
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            try:
                first = await remembered(connection, tenant, who, key, "SetBenchmark", request)
            except KeyReusedError as error:
                raise InvalidRequestError(str(error)) from error
            if first is not None:
                return pb.SetBenchmarkResponse.FromString(first)
            job = await self._job(connection, tenant, request.job_id)
            if job.status != PASSED:
                raise ConflictError(
                    f"Only a passed evaluation becomes a benchmark; this one is {job.status}"
                )
            b = BENCHMARKS.c
            where = (b.tenant_id == tenant, b.dataset_name == job.dataset_name,
                     b.splits == job.splits, b.mode == job.mode)  # fmt: skip
            values = {"job_id": job.job_id, "set_by": who, "set_at": now}
            if (await connection.execute(select(BENCHMARKS).where(*where))).one_or_none():
                await connection.execute(update(BENCHMARKS).where(*where).values(**values))
            else:
                await connection.execute(insert(BENCHMARKS).values(
                    tenant_id=tenant, dataset_name=job.dataset_name, splits=job.splits,
                    mode=job.mode, **values))  # fmt: skip
            await audit(connection, identity, action="evaluation.benchmark.set",
                        resource_type="evaluation", resource_id=job.job_id)  # fmt: skip
            row = (await connection.execute(select(BENCHMARKS).where(*where))).one()
            response = pb.SetBenchmarkResponse(benchmark=self._benchmark(row, job.dataset_version))
            await remember(connection, tenant_id=tenant, caller=who, key=key,
                           method="SetBenchmark", request=request, response=response,
                           now=now)  # fmt: skip
        return response

    async def ListBenchmarks(  # noqa: N802
        self, request: pb.ListBenchmarksRequest, context: Any
    ) -> pb.ListBenchmarksResponse:
        _, tenant, _ = await self._caller()
        b, j = BENCHMARKS.c, JOBS.c
        query = (
            select(BENCHMARKS, j.dataset_version)
            .join(JOBS, (j.tenant_id == b.tenant_id) & (j.job_id == b.job_id))
            .where(b.tenant_id == tenant)
        )
        if request.dataset_name:
            query = query.where(b.dataset_name == request.dataset_name)
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            rows = (await connection.execute(
                query.order_by(b.dataset_name, b.splits, b.mode)
            )).all()  # fmt: skip
        return pb.ListBenchmarksResponse(
            benchmarks=[self._benchmark(r, r.dataset_version) for r in rows]
        )

    @staticmethod
    def _by_scenario(attempts: list[Any]) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        for a in attempts:
            passed, planned = found.get(a.scenario_id, (0, 0))
            found[a.scenario_id] = (passed + (a.status == PASSED), planned + 1)
        return found

    async def CompareEvaluation(  # noqa: N802
        self, request: pb.CompareEvaluationRequest, context: Any
    ) -> pb.CompareEvaluationResponse:
        _, tenant, _ = await self._caller()
        async with self._engine.connect() as connection:
            await in_tenant(connection, tenant)
            job = await self._job(connection, tenant, request.job_id)
            if job.status not in FINISHED:
                raise ConflictError("Only a finished evaluation is compared")
            b = BENCHMARKS.c
            benchmark = (await connection.execute(select(BENCHMARKS).where(
                b.tenant_id == tenant, b.dataset_name == job.dataset_name,
                b.splits == job.splits, b.mode == job.mode,
            ))).one_or_none()  # fmt: skip
            if benchmark is None:
                return pb.CompareEvaluationResponse()
            base = await self._job(connection, tenant, benchmark.job_id)
            now_attempts = await self._attempts(connection, tenant, job.job_id)
            base_attempts = await self._attempts(connection, tenant, base.job_id)
        changes = rules.compare(self._by_scenario(base_attempts),
                                self._by_scenario(now_attempts))  # fmt: skip
        return pb.CompareEvaluationResponse(
            benchmark=self._benchmark(benchmark, base.dataset_version),
            scenarios=[
                pb.ScenarioComparison(
                    scenario_id=c.scenario_id,
                    benchmark_passed=c.benchmark_passed,
                    benchmark_planned=c.benchmark_planned,
                    passed=c.passed,
                    planned=c.planned,
                    change=c.change,
                )
                for c in changes
            ],
            regressed=sum(c.change == "regressed" for c in changes),
            fixed=sum(c.change == "fixed" for c in changes),
            cost_usd_change=sum(a.cost_usd for a in now_attempts)
            - sum(a.cost_usd for a in base_attempts),
            seconds_change=sum(a.seconds for a in now_attempts)
            - sum(a.seconds for a in base_attempts),
        )
