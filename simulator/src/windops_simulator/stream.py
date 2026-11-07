"""Stream the demo fleet in real time to the Telemetry Service, as its devices would (M4.4).

Runs as its own workload. Each turbine is a device with its own token, from the Secret that
`make telemetry-devices` fills, mounted as one file per turbine. Every second the fleet is
stepped forward to the wall clock, and every few seconds each device sends what it has, in one
batch, through the gateway's device route. A device keeps what it could not send and tries again,
as a real one buffers through an outage, up to an hour of samples. Light link trouble is injected
by default (SIM-004), so the data-quality checks downstream always have something to find.

    python -m windops_simulator.stream

It is configured by WINDOPS_ settings, and serves /health/live, /health/ready and /metrics.
"""

import contextlib
import heapq
import http.client
import json
import logging
import math
import os
import signal
import ssl
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from windops_simulator.delivery import DataProblems, Link
from windops_simulator.fleet import DEMO_TURBINES, TurbineSpec
from windops_simulator.scenarios import LIGHT
from windops_simulator.simulator import Simulator, random_stream

logger = logging.getLogger("windops.simulator")

# The Telemetry Service takes at most this many samples in one request.
MAXIMUM_BATCH = 600
# A device keeps at most an hour of samples while it cannot send them.
MAXIMUM_BUFFER = 3600
# Further behind than this, the fleet jumps to now in one step, leaving a gap as a real outage does.
MAXIMUM_CATCH_UP_SECONDS = 600
MAXIMUM_BACKOFF_SECONDS = 30.0
TOKEN_RELOAD_SECONDS = 60.0
# Liveness fails if the loop has not turned for this long.
STALL_SECONDS = 30.0
PROBLEMS = {"none": DataProblems(), "light": LIGHT}

Key = tuple[str, str]


@dataclass(frozen=True)
class Settings:
    ingest_url: str
    tokens_dir: Path
    ca_file: Path
    seed: int = 1001
    batch_seconds: float = 5.0
    problems: str = "light"
    http_port: int = 8080

    @classmethod
    def from_environment(cls, environment: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environment is None else environment
        problems = env.get("WINDOPS_SIMULATOR_DATA_PROBLEMS", "light")
        if problems not in PROBLEMS:
            raise ValueError(f"WINDOPS_SIMULATOR_DATA_PROBLEMS is one of: {', '.join(PROBLEMS)}")
        url = env.get("WINDOPS_INGEST_URL", "")
        if urlsplit(url).scheme != "https":
            raise ValueError("WINDOPS_INGEST_URL must be an https:// address")
        return cls(
            ingest_url=url,
            tokens_dir=Path(env.get("WINDOPS_SIMULATOR_TOKENS_DIR", "/var/run/windops/devices")),
            ca_file=Path(env.get("WINDOPS_TLS_DIR", "/var/run/windops/tls")) / "ca.crt",
            seed=int(env.get("WINDOPS_SIMULATOR_SEED", "1001")),
            batch_seconds=float(env.get("WINDOPS_SIMULATOR_BATCH_SECONDS", "5")),
            problems=problems,
            http_port=int(env.get("WINDOPS_HTTP_PORT", "8080")),
        )


def load_tokens(directory: Path) -> dict[Key, str]:
    """Each file is one turbine's token, named <tenant>.<turbine>, as the Secret's keys are."""
    tokens: dict[Key, str] = {}
    if not directory.is_dir():
        return tokens
    for path in sorted(directory.iterdir()):
        # A mounted Secret also holds ..data and dated folders, which are not tokens.
        if path.name.startswith(".") or not path.is_file():
            continue
        tenant, _, turbine = path.name.partition(".")
        token = path.read_text().strip()
        if tenant and turbine and token:
            tokens[(tenant, turbine)] = token
    return tokens


@dataclass(frozen=True)
class Answer:
    status: int
    body: dict[str, Any]
    retry_after: float | None = None


class Transport(Protocol):
    def post(self, token: str, body: bytes) -> Answer: ...


class HttpsTransport:
    """One kept-alive HTTPS connection to the gateway, checked against our own CA."""

    def __init__(self, url: str, ca_file: Path, timeout: float = 10.0) -> None:
        parts = urlsplit(url)
        self._host, self._port = parts.hostname or "", parts.port or 443
        self._path = parts.path or "/"
        self._context = ssl.create_default_context(cafile=str(ca_file))
        self._timeout = timeout
        self._connection: http.client.HTTPSConnection | None = None

    def _connect(self) -> http.client.HTTPSConnection:
        if self._connection is None:
            self._connection = http.client.HTTPSConnection(
                self._host, self._port, context=self._context, timeout=self._timeout
            )
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def post(self, token: str, body: bytes) -> Answer:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for attempt in range(2):
            try:
                connection = self._connect()
                connection.request("POST", self._path, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read()
            except (http.client.HTTPException, OSError):
                # The gateway may have closed a kept-alive connection. Try once more on a new one.
                self.close()
                if attempt:
                    raise
                continue
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            retry = response.getheader("Retry-After")
            return Answer(
                response.status,
                parsed if isinstance(parsed, dict) else {},
                float(retry) if retry and retry.isdigit() else None,
            )
        raise OSError("unreachable")  # pragma: no cover


@dataclass
class Device:
    key: Key
    token: str
    buffer: deque[Any] = field(default_factory=deque)
    next_attempt: float = 0.0
    failures: int = 0


class Metrics:
    """Counters and gauges, written in the Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def add(self, name: str, amount: float = 1, /, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + amount

    def set(self, name: str, value: float, /, **labels: str) -> None:
        with self._lock:
            self.gauges[(name, tuple(sorted(labels.items())))] = value

    def get(self, name: str, /, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            return self.counters.get(key, self.gauges.get(key, 0))

    def render(self) -> str:
        lines = []
        with self._lock:
            for kind, values in (("counter", self.counters), ("gauge", self.gauges)):
                for name in sorted({name for name, _ in values}):
                    lines.append(f"# TYPE {name} {kind}")
                    for (metric, labels), value in sorted(values.items()):
                        if metric == name:
                            text = ",".join(f'{k}="{v}"' for k, v in labels)
                            # repr keeps every digit: a timestamp must not be rounded.
                            lines.append(f"{name}{{{text}}} {float(value)!r}")
        return "\n".join(lines) + "\n"


def _labels(key: Key) -> dict[str, str]:
    return {"tenant_id": key[0], "turbine_id": key[1]}


class Streamer:
    """The fleet, its devices' links, and what each device still has to send."""

    def __init__(
        self,
        settings: Settings,
        transport: Transport,
        *,
        turbines: Iterable[TurbineSpec] = DEMO_TURBINES,
        tokens: Callable[[], dict[Key, str]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings, self.transport, self.clock = settings, transport, clock
        self._tokens = tokens or (lambda: load_tokens(settings.tokens_dir))
        self.metrics = Metrics()
        start = datetime.fromtimestamp(math.floor(clock()), UTC)
        self.specs = {(spec.tenant_id, spec.turbine_id): spec for spec in turbines}
        self.simulator = Simulator(settings.seed, list(self.specs.values()), start)
        problems = PROBLEMS[settings.problems]
        self.links = {
            key: Link(problems, random_stream(settings.seed, f"live-link:{key[0]}:{key[1]}"))
            for key in self.specs
        }
        # Deliveries waiting for their moment to be sent: (sent_at, order, device, payload).
        self._waiting: list[tuple[float, int, Key, Any]] = []
        self._order = 0
        self.devices: dict[Key, Device] = {}
        self._tokens_loaded_at = -math.inf
        self._last_batch = -math.inf
        self.heartbeat = clock()
        for key in self.specs:
            for name in ("accepted", "rejected", "generated"):
                self.metrics.add(f"windops_simulator_samples_{name}_total", 0, **_labels(key))

    # --- devices

    def reload_tokens(self) -> None:
        tokens = self._tokens()
        for key, token in tokens.items():
            if key not in self.specs:
                continue
            if key in self.devices:
                self.devices[key].token = token
            else:
                self.devices[key] = Device(key, token)
                logger.info("Device ready", extra={"tenant_id": key[0], "turbine_id": key[1]})
        missing = sorted(f"{t}/{u}" for t, u in self.specs if (t, u) not in tokens)
        if missing and not self.devices:
            logger.warning("No device tokens yet", extra={"missing": missing})
        self._tokens_loaded_at = self.clock()

    @property
    def in_flight(self) -> int:
        """Samples measured but not yet due to be sent, such as delayed ones."""
        return len(self._waiting)

    @property
    def ready(self) -> bool:
        return bool(self.devices)

    # --- time

    def advance(self) -> None:
        """Step the fleet up to the wall clock, and release whatever is due to be sent."""
        now = self.clock()
        behind = now - self.simulator.now.timestamp()
        if behind > MAXIMUM_CATCH_UP_SECONDS:
            logger.warning("Fell behind, so jumping to now", extra={"seconds": int(behind)})
            self._carry(self.simulator.step(math.floor(behind)))
        while self.simulator.now.timestamp() + 1 <= now:
            self._carry(self.simulator.step(1.0))
        while self._waiting and self._waiting[0][0] <= now:
            _, _, key, payload = heapq.heappop(self._waiting)
            device = self.devices.get(key)
            if device is None:
                self.metrics.add("windops_simulator_samples_dropped_total", reason="no_token",
                                 **_labels(key))  # fmt: skip
                continue
            device.buffer.append(payload)
            if len(device.buffer) > MAXIMUM_BUFFER:
                device.buffer.popleft()
                self.metrics.add(
                    "windops_simulator_samples_dropped_total", reason="buffer_full", **_labels(key)
                )
        for key, device in self.devices.items():
            self.metrics.set("windops_simulator_buffered_samples", len(device.buffer),
                             **_labels(key))  # fmt: skip
        self.heartbeat = now

    def _carry(self, samples: list[Any]) -> None:
        for sample in samples:
            payload = sample.as_dict()
            # The sample's Unix second: one a second, and still rising after a restart.
            payload["sequence"] = int(sample.event_time.timestamp())
            key = (sample.tenant_id, sample.turbine_id)
            self.metrics.add("windops_simulator_samples_generated_total", **_labels(key))
            for delivery in self.links[key].carry(payload, sample.event_time):
                self._order += 1
                heapq.heappush(
                    self._waiting,
                    (delivery.sent_at.timestamp(), self._order, key, delivery.payload),
                )

    # --- sending

    def send_due(self) -> None:
        """Send each device's batch, if a batch is due and the device is not waiting."""
        now = self.clock()
        if now - self._last_batch < self.settings.batch_seconds:
            return
        self._last_batch = now
        for device in self.devices.values():
            if device.buffer and device.next_attempt <= now:
                self._send(device, now)

    def _send(self, device: Device, now: float) -> None:
        batch = [device.buffer[index] for index in range(min(MAXIMUM_BATCH, len(device.buffer)))]
        body = json.dumps({"samples": batch}).encode()
        labels = _labels(device.key)
        try:
            answer = self.transport.post(device.token, body)
        except OSError as error:
            self.metrics.add("windops_simulator_requests_total", status="unreachable")
            self._back_off(device, now, type(error).__name__)
            return
        self.metrics.add("windops_simulator_requests_total", status=str(answer.status))
        if answer.status == 200:
            for _ in batch:
                device.buffer.popleft()
            device.failures, device.next_attempt = 0, 0.0
            rejected = len(answer.body.get("rejected", []))
            self.metrics.add("windops_simulator_samples_accepted_total",
                             answer.body.get("accepted", 0), **labels)  # fmt: skip
            self.metrics.add("windops_simulator_samples_rejected_total", rejected, **labels)
            self.metrics.set("windops_simulator_last_accepted_timestamp_seconds", now, **labels)
        elif answer.status == 429:
            device.next_attempt = now + (answer.retry_after or self.settings.batch_seconds)
        elif answer.status in (400, 401, 403, 413):
            # Sending these again cannot succeed. A refused token may have been replaced.
            for _ in batch:
                device.buffer.popleft()
            self.metrics.add("windops_simulator_samples_dropped_total", len(batch),
                             reason=f"refused_{answer.status}", **labels)  # fmt: skip
            logger.warning(
                "The Telemetry Service refused a batch",
                extra={**labels, "status": answer.status, "code": answer.body.get("code", "")},
            )
            if answer.status == 401:
                device.next_attempt = now + TOKEN_RELOAD_SECONDS
                self._tokens_loaded_at = -math.inf
        else:
            self._back_off(device, now, f"HTTP {answer.status}")

    def _back_off(self, device: Device, now: float, reason: str) -> None:
        device.failures += 1
        wait = min(MAXIMUM_BACKOFF_SECONDS, float(2 ** min(device.failures, 5)))
        device.next_attempt = now + wait
        if device.failures in (1, 5) or device.failures % 20 == 0:
            logger.warning("Could not send, keeping the samples",
                           extra={**_labels(device.key), "reason": reason,
                                  "failures": device.failures})  # fmt: skip

    def flush(self) -> None:
        """A last try for what is still buffered, before stopping."""
        self._last_batch = -math.inf
        for device in self.devices.values():
            device.next_attempt = 0.0
        self.send_due()

    # --- the loop

    def tick(self) -> None:
        if self.clock() - self._tokens_loaded_at >= TOKEN_RELOAD_SECONDS:
            self.reload_tokens()
        self.advance()
        self.send_due()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception as error:  # Keep streaming; one bad turn is logged, not fatal.
                logger.exception("A turn failed", extra={"error_type": type(error).__name__})
            stop.wait(max(0.05, 1 - (self.clock() % 1)))


def health_server(streamer: Streamer, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # The name comes from http.server.
            if self.path == "/health/live":
                alive = streamer.clock() - streamer.heartbeat < STALL_SECONDS
                self._answer(200 if alive else 503, "ok\n" if alive else "stalled\n")
            elif self.path == "/health/ready":
                self._answer(200 if streamer.ready else 503,
                             "ok\n" if streamer.ready else "no device tokens\n")  # fmt: skip
            elif self.path == "/metrics":
                self._answer(200, streamer.metrics.render(), "text/plain; version=0.0.4")
            else:
                self._answer(404, "not found\n")

        def _answer(self, status: int, text: str, kind: str = "text/plain") -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return  # Probes every few seconds would drown the logs.

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104  # Probes come from outside the pod.


class _JsonFormatter(logging.Formatter):
    _SKIP = frozenset(vars(logging.makeLogRecord({})))

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,
            "service": "simulator",
            "message": record.getMessage(),
            **{k: v for k, v in vars(record).items() if k not in self._SKIP},
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def main() -> int:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=os.environ.get("WINDOPS_LOG_LEVEL", "INFO"), handlers=[handler])
    settings = Settings.from_environment()
    transport = HttpsTransport(settings.ingest_url, settings.ca_file)
    streamer = Streamer(settings, transport)
    server = health_server(streamer, settings.http_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    with contextlib.suppress(ValueError):  # Signals can be set only in the main thread.
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: stop.set())
    logger.info(
        "Streaming the demo fleet",
        extra={"turbines": len(streamer.specs), "seed": settings.seed,
               "batch_seconds": settings.batch_seconds, "data_problems": settings.problems},
    )  # fmt: skip
    streamer.run(stop)
    streamer.flush()
    server.shutdown()
    transport.close()
    logger.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
