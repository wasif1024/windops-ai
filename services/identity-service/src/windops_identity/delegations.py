"""The delegation calls: start one, mint its agents' tokens, end it, and publish the keys.

Each call is let in only by the certificate of the service that should make it:

| Call | Callers | Identity |
| --- | --- | --- |
| StartDelegation, for a user | Web BFF, Investigation | That user, with their live Keycloak token |
| StartDelegation, for fault-investigator | Investigation | The Investigation Service, as itself |
| IssueAgentToken | Agent Runtime | Any, in the delegation's tenant |
| EndDelegation | Web BFF, Investigation, Agent Runtime | Any, in the delegation's tenant |
| GetAgentTokenKeys | Any service | Any |

Before each token, a user's delegation is checked again at Keycloak: a user disabled, or no longer
in the tenant, gets no further token. The check is remembered for a minute.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from windops.events.v1 import events_pb2
from windops.identity.v1 import identity_pb2
from windops_auth import current_peer_service
from windops_events import audit_request_subject
from windops_grpc import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnavailableError,
    current_identity,
)
from windops_migrations import in_tenant
from windops_outbox import add_event

from windops_identity.agents import (
    AGENTS,
    DEFAULT_DELEGATION_SECONDS,
    DELEGATIONS,
    FAULT_INVESTIGATOR,
    MAXIMUM_DELEGATION_SECONDS,
    MINTERS,
    STARTERS,
    USER_PREFIX,
    SigningKey,
    mint,
    new_delegation_id,
)
from windops_identity.keycloak import KeycloakAdmin, KeycloakError
from windops_identity.store import SERVICE_NAME
from windops_identity.targets import EXCHANGE_TARGETS, MCP_PREFIX

INVESTIGATION = "service:investigation"
_DELEGATION = re.compile(r"dlg_[a-z0-9]{20}")
_CASE = re.compile(r"case_[a-z0-9]{20}")
_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_MAXIMUM_TOKEN = 16_384
_MAXIMUM_REASON = 500
STILL_VALID_SECONDS = 60.0


def _stamp(value: datetime | None) -> Timestamp | None:
    if value is None:
        return None
    stamp = Timestamp()
    stamp.FromDatetime(value if value.tzinfo else value.replace(tzinfo=UTC))
    return stamp


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def delegation_message(row: Any) -> identity_pb2.Delegation:
    return identity_pb2.Delegation(
        delegation_id=row.delegation_id, principal=row.principal, case_id=row.case_id,
        created_at=_stamp(row.created_at), expires_at=_stamp(row.expires_at),
        ended_at=_stamp(row.ended_at), end_reason=row.end_reason, started_by=row.started_by,
    )  # fmt: skip


async def _audit(
    connection: AsyncConnection, *, tenant_id: str, action: str, actor_id: str, delegation: str,
    on_behalf_of: str = "",
) -> None:  # fmt: skip
    await add_event(
        connection,
        audit_request_subject(SERVICE_NAME),
        tenant_id=tenant_id,
        payload=events_pb2.AuditRequested(
            action=action,
            actor_type="service",
            actor_id=actor_id,
            on_behalf_of=on_behalf_of,
            resource_type="delegation",
            resource_id=delegation,
            outcome="succeeded",
        ),
        producer=SERVICE_NAME,
    )


class Delegations:
    """The delegation calls, mixed into the Identity Service."""

    _engine: AsyncEngine
    _keycloak: KeycloakAdmin

    def _setup_delegations(
        self,
        signing_key: SigningKey | None,
        issuer: str,
        *,
        peer: Callable[[], str | None] = current_peer_service,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._signing_key, self._issuer, self._peer, self._now = signing_key, issuer, peer, clock
        self._still_valid: dict[tuple[str, str], float] = {}

    def _from(self, allowed: frozenset[str]) -> str:
        peer = self._peer()
        if peer not in allowed:
            raise PermissionDeniedError("This service may not make this call")
        return peer

    async def StartDelegation(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.StartDelegationRequest, context: Any
    ) -> identity_pb2.StartDelegationResponse:
        peer = self._from(STARTERS)
        identity = current_identity()
        if not _CASE.fullmatch(request.case_id):
            raise InvalidRequestError("case_id must be a case's ID")
        if not _KEY.fullmatch(request.idempotency_key):
            raise InvalidRequestError("idempotency_key must be 8 to 128 letters, digits or ._:-")
        seconds = request.ttl_seconds or DEFAULT_DELEGATION_SECONDS
        if not 60 <= seconds <= MAXIMUM_DELEGATION_SECONDS:
            raise InvalidRequestError(
                f"ttl_seconds must be from 60 to {MAXIMUM_DELEGATION_SECONDS}"
            )
        if request.subject_token:
            principal = await self._user_principal(request.subject_token, identity)
        else:
            # A fault-triggered investigation has no person behind it (SRS 9.2).
            if peer != "windops-investigation" or identity.subject != INVESTIGATION:
                raise PermissionDeniedError(
                    "Only the Investigation Service, acting as itself, may act as the "
                    "fault-investigator"
                )
            principal = FAULT_INVESTIGATOR
        # The token is left out: it proves who the user is, and is never kept.
        fingerprint = hashlib.sha256(
            json.dumps([principal, request.case_id, seconds], separators=(",", ":")).encode()
        ).hexdigest()
        tenant, now = identity.tenant_id, self._now()
        try:
            async with self._engine.begin() as connection:
                await in_tenant(connection, tenant)
                d = DELEGATIONS.c
                existing = (
                    await connection.execute(
                        select(DELEGATIONS).where(
                            d.tenant_id == tenant, d.started_by == peer,
                            d.idempotency_key == request.idempotency_key,
                        )
                    )
                ).one_or_none()  # fmt: skip
                if existing is not None:
                    if existing.request_hash != fingerprint:
                        raise ConflictError("That idempotency_key was used for another delegation")
                    return identity_pb2.StartDelegationResponse(
                        delegation=delegation_message(existing)
                    )
                delegation_id = new_delegation_id()
                await connection.execute(
                    DELEGATIONS.insert().values(
                        tenant_id=tenant, delegation_id=delegation_id, principal=principal,
                        case_id=request.case_id, started_by=peer, created_at=now,
                        expires_at=now + timedelta(seconds=seconds),
                        idempotency_key=request.idempotency_key, request_hash=fingerprint,
                    )
                )  # fmt: skip
                await _audit(
                    connection, tenant_id=tenant, action="identity.delegation.started",
                    actor_id=f"service:{peer.removeprefix('windops-')}", delegation=delegation_id,
                    on_behalf_of=principal,
                )  # fmt: skip
                row = (
                    await connection.execute(
                        select(DELEGATIONS).where(
                            d.tenant_id == tenant, d.delegation_id == delegation_id
                        )
                    )
                ).one()
                return identity_pb2.StartDelegationResponse(delegation=delegation_message(row))
        except IntegrityError as error:
            raise ConflictError("A concurrent call with that key won: retry it") from error

    async def _user_principal(self, token: str, identity: Any) -> str:
        if len(token) > _MAXIMUM_TOKEN:
            raise InvalidRequestError("The token is too long")
        if not identity.user_id:
            raise PermissionDeniedError("Only the user themselves may delegate to agents")
        try:
            owner = await self._keycloak.introspect(token)
        except KeycloakError as error:
            raise UnavailableError("Keycloak cannot be reached") from error
        # Keycloak's answer, not the token's own words, so a forged token proves nothing.
        if (
            owner.get("active") is not True
            or owner.get("sub") != identity.user_id
            or owner.get("tenant_id") != identity.tenant_id
        ):
            raise PermissionDeniedError("Only the caller's own, current token can delegate")
        return f"{USER_PREFIX}{identity.user_id}"

    async def _load(self, connection: AsyncConnection, tenant: str, delegation_id: str) -> Any:
        if not _DELEGATION.fullmatch(delegation_id):
            raise InvalidRequestError("delegation_id must be a delegation's ID")
        d = DELEGATIONS.c
        row = (
            await connection.execute(
                select(DELEGATIONS)
                .where(d.tenant_id == tenant, d.delegation_id == delegation_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("No such delegation")
        return row

    async def IssueAgentToken(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.IssueAgentTokenRequest, context: Any
    ) -> identity_pb2.IssueAgentTokenResponse:
        self._from(MINTERS)
        if self._signing_key is None:
            raise UnavailableError("Agent tokens cannot be signed: the key is not mounted")
        tenant = current_identity().tenant_id
        if request.agent not in AGENTS:
            raise InvalidRequestError("No such agent")
        if not request.audience.startswith(MCP_PREFIX) or request.audience not in EXCHANGE_TARGETS:
            raise InvalidRequestError("Agent tokens are only for MCP servers")
        scopes = sorted(set(request.scopes))
        if not scopes or any(s not in EXCHANGE_TARGETS[request.audience] for s in scopes):
            raise InvalidRequestError("Name that server's tools, and only its own")
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.delegation_id)
        if row.ended_at is not None:
            raise PermissionDeniedError("The delegation has ended")
        if _utc(row.expires_at) <= self._now():
            raise PermissionDeniedError("The delegation has expired")
        if row.principal.startswith(USER_PREFIX):
            await self._still_in_tenant(tenant, row.principal.removeprefix(USER_PREFIX))
        token, seconds = mint(
            self._signing_key, issuer=self._issuer, principal=row.principal, tenant_id=tenant,
            agent=request.agent, audience=request.audience, scopes=scopes,
        )  # fmt: skip
        return identity_pb2.IssueAgentTokenResponse(
            access_token=token, expires_in_seconds=seconds, audience=request.audience,
            scopes=scopes, actor=f"agent:{request.agent}", principal=row.principal,
        )  # fmt: skip

    async def _still_in_tenant(self, tenant: str, user: str) -> None:
        """A user disabled, or no longer in the tenant, delegates nothing more."""
        key = (tenant, user)
        checked = self._still_valid.get(key)
        if checked is not None and time.monotonic() - checked < STILL_VALID_SECONDS:
            return
        try:
            organization = await self._keycloak.organization_id(tenant)
            enabled = (await self._keycloak.user(user)).get("enabled") is True
            member = organization is not None and await self._keycloak.is_member(organization, user)
        except KeycloakError as error:
            if getattr(error, "status", None) == 404:
                raise PermissionDeniedError("The user no longer exists") from error
            raise UnavailableError("Keycloak cannot be reached") from error
        if not (enabled and member):
            self._still_valid.pop(key, None)
            raise PermissionDeniedError("The user may no longer delegate in this tenant")
        self._still_valid[key] = time.monotonic()

    async def EndDelegation(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.EndDelegationRequest, context: Any
    ) -> identity_pb2.EndDelegationResponse:
        peer = self._from(STARTERS | MINTERS)
        tenant = current_identity().tenant_id
        reason = request.reason.strip()[:_MAXIMUM_REASON] or "Ended"
        async with self._engine.begin() as connection:
            await in_tenant(connection, tenant)
            row = await self._load(connection, tenant, request.delegation_id)
            if row.ended_at is None:
                d = DELEGATIONS.c
                await connection.execute(
                    update(DELEGATIONS)
                    .where(d.tenant_id == tenant, d.delegation_id == row.delegation_id)
                    .values(ended_at=self._now(), end_reason=reason)
                )
                await _audit(
                    connection, tenant_id=tenant, action="identity.delegation.ended",
                    actor_id=f"service:{peer.removeprefix('windops-')}",
                    delegation=row.delegation_id, on_behalf_of=row.principal,
                )  # fmt: skip
                row = await self._load(connection, tenant, row.delegation_id)
            return identity_pb2.EndDelegationResponse(delegation=delegation_message(row))

    async def GetAgentTokenKeys(  # noqa: N802  # The name comes from the contract.
        self, request: identity_pb2.GetAgentTokenKeysRequest, context: Any
    ) -> identity_pb2.GetAgentTokenKeysResponse:
        if not self._peer():
            raise PermissionDeniedError("Only a service may read the keys")
        keys = self._signing_key.jwks() if self._signing_key is not None else {"keys": []}
        return identity_pb2.GetAgentTokenKeysResponse(
            issuer=self._issuer, jwks_json=json.dumps(keys)
        )
