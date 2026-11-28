"""Errors for calls refused before they reach a dependency."""


class BreakerOpenError(Exception):
    """The call was refused without reaching the dependency."""

    def __init__(self, dependency: str) -> None:
        super().__init__(f"Circuit breaker for {dependency} is open")
        self.dependency = dependency
