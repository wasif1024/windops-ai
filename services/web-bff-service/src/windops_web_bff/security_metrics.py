"""What Web BFF counts for the Security dashboard (SRS 10.3), with fixed labels only."""

from typing import Any

from opentelemetry import metrics

from windops_web_bff.limits import LIMITS
from windops_web_bff.tokens import REASONS


class SecurityCounters:
    """Made once telemetry is running, as a count made before then is lost.

    Each series starts at 0, so Prometheus sees the first refusal as a change. `start` runs as
    the service starts, and the first use starts it otherwise, as in tests.
    """

    def __init__(self, meter_provider: metrics.MeterProvider | None = None) -> None:
        self._provider = meter_provider
        self._tokens: Any = None
        self._limited: Any = None

    def start(self) -> None:
        if self._tokens is not None:
            return
        meter = (self._provider or metrics.get_meter_provider()).get_meter("windops.web_bff")
        self._tokens = meter.create_counter(
            "windops.web_bff.tokens_refused", description="Requests refused for their token"
        )
        self._limited = meter.create_counter(
            "windops.web_bff.rate_limited", description="Requests over a user's limit"
        )
        for reason in REASONS:
            self._tokens.add(0, {"reason": reason})
        for kind in LIMITS:
            self._limited.add(0, {"traffic_class": kind})

    def token_refused(self, reason: str) -> None:
        self.start()
        self._tokens.add(1, {"reason": reason})

    def rate_limited(self, traffic_class: str) -> None:
        self.start()
        self._limited.add(1, {"traffic_class": traffic_class})
