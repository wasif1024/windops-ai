"""gRPC server, client and service runtime for WindOps services (SRS section 7.4)."""

from windops_grpc.client import ServiceClient, outgoing_metadata
from windops_grpc.errors import (
    ConflictError,
    DomainError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnavailableError,
)
from windops_grpc.identity import (
    IdentityError,
    VerifiedIdentity,
    current_identity,
    sign_identity,
    verify_identity,
)
from windops_grpc.interceptor import ServerContextInterceptor
from windops_grpc.runtime import ServiceRuntime, run_service
from windops_grpc.service_identity import acting_as_service, service_subject
from windops_grpc.settings import Settings, SettingsError
from windops_grpc.tls import create_channel

__all__ = [
    "ConflictError",
    "DomainError",
    "IdentityError",
    "InvalidRequestError",
    "NotFoundError",
    "PermissionDeniedError",
    "ServerContextInterceptor",
    "ServiceClient",
    "ServiceRuntime",
    "Settings",
    "SettingsError",
    "UnavailableError",
    "VerifiedIdentity",
    "acting_as_service",
    "create_channel",
    "current_identity",
    "outgoing_metadata",
    "run_service",
    "service_subject",
    "sign_identity",
    "verify_identity",
]
