"""Map service errors to gRPC status codes in one consistent way (GRP-006)."""

import grpc


class DomainError(Exception):
    """An expected error whose message is safe to return to the caller."""

    code = grpc.StatusCode.UNKNOWN


class InvalidRequestError(DomainError):
    code = grpc.StatusCode.INVALID_ARGUMENT


class NotFoundError(DomainError):
    code = grpc.StatusCode.NOT_FOUND


class PermissionDeniedError(DomainError):
    code = grpc.StatusCode.PERMISSION_DENIED


class ConflictError(DomainError):
    code = grpc.StatusCode.FAILED_PRECONDITION


class UnavailableError(DomainError):
    code = grpc.StatusCode.UNAVAILABLE
