"""Turn a service's gRPC answer into the HTTP one a browser understands (RFC 9457 problems)."""

from typing import Any

import grpc
from fastapi.responses import JSONResponse
from windops_auth import MFA_LEVEL_SECONDS, STEP_UP_MESSAGE

_STATUS = {
    grpc.StatusCode.INVALID_ARGUMENT: (400, "invalid_request", "The request is not valid"),
    grpc.StatusCode.UNAUTHENTICATED: (401, "not_signed_in", "Sign in again"),
    grpc.StatusCode.PERMISSION_DENIED: (403, "forbidden", "You do not have permission to do this"),
    grpc.StatusCode.NOT_FOUND: (404, "not_found", "Not found"),
    grpc.StatusCode.FAILED_PRECONDITION: (409, "conflict", "This conflicts with the current state"),
    grpc.StatusCode.RESOURCE_EXHAUSTED: (429, "too_many_requests", "Too many requests"),
    grpc.StatusCode.UNAVAILABLE: (503, "unavailable", "Temporarily unavailable"),
    grpc.StatusCode.DEADLINE_EXCEEDED: (503, "unavailable", "Temporarily unavailable"),
}
_UNEXPECTED = (502, "bad_gateway", "Something went wrong")


def problem(status: int, title: str, code: str = "", **headers: str) -> JSONResponse:
    """An RFC 9457 problem. Every word in it is ours, never text from an exception or a service."""
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status}
    if code:
        body["code"] = code
    return JSONResponse(
        body, status_code=status, headers=headers, media_type="application/problem+json"
    )


def step_up() -> JSONResponse:
    """Ask the browser to sign in again with a second factor (RFC 9470), not to give up."""
    challenge = (
        'Bearer error="insufficient_user_authentication", '
        f'error_description="{STEP_UP_MESSAGE}", acr_values="mfa", max_age="{MFA_LEVEL_SECONDS}"'
    )
    return problem(401, STEP_UP_MESSAGE, "step_up_required", **{"WWW-Authenticate": challenge})


def from_grpc(error: grpc.aio.AioRpcError) -> JSONResponse:
    """A service's message is not forwarded, so nothing internal can reach the browser by it.

    The step-up refusal is recognised by comparing with our own constant, and only that is sent.
    """
    if error.code() is grpc.StatusCode.PERMISSION_DENIED and error.details() == STEP_UP_MESSAGE:
        return step_up()
    status, code, title = _STATUS.get(error.code(), _UNEXPECTED)
    return problem(status, title, code)
