"""Agent identities: delegations, and the short tokens agents call MCP tools with (ADR 0019).

A delegation lets a run's agents act for one principal, in one tenant, until it ends or expires:
the user who started the investigation, or the tenant's fault-investigator (SRS 9.2). An agent
token names that principal as its subject and the agent in "act" (RFC 8693). It is for one MCP
server and the named tools, and lasts 120 seconds (SEC-008). The Identity Service signs it with
its own ES256 key, whose public half the MCP servers fetch over mutual TLS.
"""

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import Column, DateTime, Index, MetaData, Table, Text

from windops_identity.store import METADATA as STORE_METADATA
from windops_identity.targets import AGENT_TOOLS

AGENT_TOKEN_SECONDS = 120
DEFAULT_DELEGATION_SECONDS = 3600
MAXIMUM_DELEGATION_SECONDS = 4 * 3600
FAULT_INVESTIGATOR = "service:fault-investigator"
USER_PREFIX = "user:"
AGENT_PREFIX = "agent:"
# The agents of SRS 5.3, each with its own allowlist. A token names one of them, and only one.
AGENTS = frozenset(AGENT_TOOLS)
# Who may start a delegation, and who may mint its tokens, by the certificate they present.
STARTERS = frozenset({"windops-web-bff", "windops-investigation"})
MINTERS = frozenset({"windops-agent-runtime"})
METADATA: MetaData = STORE_METADATA

DELEGATIONS = Table(
    "delegations",
    METADATA,
    Column("tenant_id", Text, primary_key=True),
    Column("delegation_id", Text, primary_key=True),
    Column("principal", Text, nullable=False),
    Column("case_id", Text, nullable=False),
    Column("started_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("end_reason", Text, nullable=False, server_default=""),
    Column("idempotency_key", Text, nullable=False),
    Column("request_hash", Text, nullable=False),
    Index("delegations_one_key", "tenant_id", "started_by", "idempotency_key", unique=True),
)


def new_delegation_id() -> str:
    return "dlg_" + "".join(
        secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(20)
    )


@dataclass(frozen=True)
class SigningKey:
    private_key: ec.EllipticCurvePrivateKey
    key_id: str

    @classmethod
    def from_pem(cls, pem: bytes) -> "SigningKey":
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("The agent token key must be an ECDSA P-256 private key")
        public = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        key_id = base64.urlsafe_b64encode(hashlib.sha256(public).digest()).decode().rstrip("=")
        return cls(private_key=key, key_id=key_id[:32])

    @classmethod
    def from_file(cls, path: Path) -> "SigningKey":
        return cls.from_pem(path.read_bytes())

    def jwks(self) -> dict[str, Any]:
        """The public key only, as a JWKS."""
        public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.private_key.public_key()))
        return {"keys": [{**public, "kid": self.key_id, "use": "sig", "alg": "ES256"}]}


def mint(
    key: SigningKey,
    *,
    issuer: str,
    principal: str,
    tenant_id: str,
    agent: str,
    audience: str,
    scopes: list[str],
    now: float | None = None,
) -> tuple[str, int]:
    """A token for one agent, acting for the principal, at one MCP server's named tools."""
    issued = int(time.time() if now is None else now)
    subject = principal.removeprefix(USER_PREFIX)
    claims: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "tenant_id": tenant_id,
        "scope": " ".join(sorted(scopes)),
        "act": {"sub": f"{AGENT_PREFIX}{agent}"},
        "iat": issued,
        "nbf": issued,
        "exp": issued + AGENT_TOKEN_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(claims, key.private_key, algorithm="ES256", headers={"kid": key.key_id})
    return token, AGENT_TOKEN_SECONDS
