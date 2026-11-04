import grpc
import pytest
from conftest import FakeRpcError
from windops_resilience import is_dependency_failure


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.INTERNAL,
    ],
)
def test_srs_failure_codes_count(code: grpc.StatusCode) -> None:
    assert is_dependency_failure(FakeRpcError(code))


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
    ],
)
def test_srs_caller_codes_never_count(code: grpc.StatusCode) -> None:
    assert not is_dependency_failure(FakeRpcError(code))


class ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(str(status_code))
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, True), (500, True), (503, True), (599, True), (400, False), (401, False), (404, False)],
)
def test_provider_status_codes(status: int, expected: bool) -> None:
    assert is_dependency_failure(ProviderError(status)) is expected


def test_nested_http_response_status() -> None:
    class Response:
        status_code = 502

    class HttpError(Exception):
        response = Response()

    assert is_dependency_failure(HttpError())


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError()])
def test_network_errors_count(error: Exception) -> None:
    assert is_dependency_failure(error)


def test_programming_errors_do_not_count() -> None:
    assert not is_dependency_failure(ValueError("bug"))
