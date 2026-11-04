"""Shared test helpers."""

import grpc


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode) -> None:
        super().__init__(code.name)
        self._code = code

    def code(self) -> grpc.StatusCode:
        return self._code
