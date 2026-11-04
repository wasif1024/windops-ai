"""Decide whether an error means a dependency is unhealthy (SRS section 7.7)."""

# gRPC status names that count as a dependency failure. INTERNAL is included because a
# caller only sees it for an unexpected server error.
FAILURE_GRPC_CODES = frozenset(
    {"UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "INTERNAL"}
)


def _grpc_code_name(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    if not callable(code):
        return None
    value = code()
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else None


def _http_status(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_dependency_failure(error: BaseException) -> bool:
    """Return whether an error should count against a dependency's circuit breaker.

    Caller errors such as INVALID_ARGUMENT, NOT_FOUND, PERMISSION_DENIED and UNAUTHENTICATED
    show the dependency is responding, so they return False.
    """
    code = _grpc_code_name(error)
    if code is not None:
        return code in FAILURE_GRPC_CODES
    status = _http_status(error)
    if status is not None:
        return status == 429 or 500 <= status <= 599
    return isinstance(error, TimeoutError | ConnectionError)
