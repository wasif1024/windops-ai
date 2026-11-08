"""The live stream: devices batch, buffer through outages and respect the Telemetry Service."""

import json
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from windops_simulator.fleet import DEMO_TURBINES
from windops_simulator.stream import (
    MAXIMUM_BUFFER,
    MAXIMUM_CMS_BUFFER,
    Answer,
    Device,
    Metrics,
    Settings,
    Streamer,
    health_server,
    load_tokens,
    routes_for,
)

START = 1_800_000_000.0
ONE = tuple(spec for spec in DEMO_TURBINES if spec.turbine_id == "dogger-east-t01")
TWO = tuple(
    spec for spec in DEMO_TURBINES if spec.turbine_id in {"dogger-east-t01", "atlas-ridge-t01"}
)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeTransport:
    """Records each post, and answers from a script, then with 200."""

    script: list[Answer | Exception] = field(default_factory=list)
    posts: list[tuple[str, list[Any]]] = field(default_factory=list)

    cms: list[dict[str, Any]] = field(default_factory=list)

    def post(self, token: str, body: bytes, kind: str = "samples") -> Answer:
        if kind == "cms":
            self.cms.append(json.loads(body))
            return Answer(200, {"accepted": 3, "rejected": []})
        samples = json.loads(body)["samples"]
        self.posts.append((token, samples))
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return Answer(200, {"accepted": len(samples), "rejected": []})


def settings(problems: str = "none", batch: float = 5.0) -> Settings:
    return Settings(
        ingest_url="https://gateway/ingest/v1/samples",
        tokens_dir=Path("/nowhere"),
        ca_file=Path("/nowhere/ca.crt"),
        batch_seconds=batch,
        problems=problems,
    )


def streamer(
    transport: FakeTransport, clock: Clock, *, turbines: Any = ONE, problems: str = "none"
) -> Streamer:
    tokens = {(spec.tenant_id, spec.turbine_id): f"token-{spec.turbine_id}" for spec in turbines}
    return Streamer(
        settings(problems), transport, turbines=turbines, tokens=lambda: dict(tokens), clock=clock
    )


def run(stream: Streamer, clock: Clock, seconds: int) -> None:
    for _ in range(seconds):
        clock.now += 1
        stream.tick()


def sent(transport: FakeTransport) -> list[Any]:
    return [sample for _, samples in transport.posts for sample in samples]


def test_each_device_sends_only_its_turbine_in_batches_with_its_own_token() -> None:
    transport, clock = FakeTransport(), Clock()
    stream = streamer(transport, clock, turbines=TWO)
    run(stream, clock, 20)
    assert {token for token, _ in transport.posts} == {
        "token-dogger-east-t01",
        "token-atlas-ridge-t01",
    }
    for token, samples in transport.posts:
        assert {s["turbine_id"] for s in samples} == {token.removeprefix("token-")}
        assert all(s["synthetic"] is True for s in samples)
    # About one batch each 5 seconds, per device.
    assert 6 <= len(transport.posts) <= 10


def test_a_samples_sequence_is_its_unix_second_so_it_keeps_rising_after_a_restart() -> None:
    transport, clock = FakeTransport(), Clock()
    run(streamer(transport, clock), clock, 12)
    samples = sent(transport)
    for sample in samples:
        moment = datetime.fromisoformat(sample["event_time"])
        assert sample["sequence"] == int(moment.timestamp())
    assert [s["sequence"] for s in samples] == list(
        range(int(START) + 1, int(START) + 1 + len(samples))
    )
    assert datetime.fromisoformat(samples[-1]["event_time"]) <= datetime.fromtimestamp(
        clock.now, UTC
    )


def test_samples_are_kept_through_an_outage_and_none_is_lost() -> None:
    script: list[Answer | Exception] = [OSError("down")] * 3
    transport, clock = FakeTransport([*script, Answer(503, {})]), Clock()
    stream = streamer(transport, clock)
    run(stream, clock, 120)
    labels = {"tenant_id": "north-sea", "turbine_id": "dogger-east-t01"}
    accepted = stream.metrics.get("windops_simulator_samples_accepted_total", **labels)
    generated = stream.metrics.get("windops_simulator_samples_generated_total", **labels)
    buffered = len(stream.devices[("north-sea", "dogger-east-t01")].buffer)
    assert accepted + buffered + stream.in_flight == generated
    # The first batch to get through starts where the first failed one did, and stays in order.
    first = transport.posts[0][1][0]["sequence"]
    assert transport.posts[4][1][0]["sequence"] == first
    sequences = [s["sequence"] for _, samples in transport.posts[4:] for s in samples]
    assert sequences == sorted(sequences) == list(range(first, first + len(sequences)))


def test_a_rate_limited_device_waits_as_long_as_it_is_told() -> None:
    transport, clock = FakeTransport([Answer(429, {}, retry_after=40)]), Clock()
    stream = streamer(transport, clock)
    run(stream, clock, 30)
    assert len(transport.posts) == 1
    run(stream, clock, 20)
    assert len(transport.posts) == 2


def test_a_refused_credential_drops_the_batch_and_reloads_the_tokens() -> None:
    transport, clock = FakeTransport([Answer(401, {"code": "bad_credential"})]), Clock()
    loads = []
    tokens = {("north-sea", "dogger-east-t01"): "old"}

    def provider() -> dict[tuple[str, str], str]:
        loads.append(clock.now)
        return dict(tokens)

    stream = Streamer(settings(), transport, turbines=ONE, tokens=provider, clock=clock)
    run(stream, clock, 6)
    assert stream.metrics.get(
        "windops_simulator_samples_dropped_total", reason="refused_401",
        tenant_id="north-sea", turbine_id="dogger-east-t01",
    ) > 0  # fmt: skip
    tokens[("north-sea", "dogger-east-t01")] = "new"
    run(stream, clock, 70)
    assert len(loads) >= 2
    assert transport.posts[-1][0] == "new"


def test_the_buffer_keeps_only_the_newest_hour() -> None:
    transport, clock = FakeTransport([OSError("down")] * 10_000), Clock()
    stream = streamer(transport, clock)
    run(stream, clock, MAXIMUM_BUFFER + 51)
    device = stream.devices[("north-sea", "dogger-east-t01")]
    assert len(device.buffer) == MAXIMUM_BUFFER
    assert stream.metrics.get(
        "windops_simulator_samples_dropped_total", reason="buffer_full",
        tenant_id="north-sea", turbine_id="dogger-east-t01",
    ) >= 50  # fmt: skip


def test_after_a_long_pause_the_fleet_jumps_to_now_leaving_a_gap() -> None:
    transport, clock = FakeTransport(), Clock()
    stream = streamer(transport, clock)
    run(stream, clock, 3)
    clock.now += 3600
    stream.tick()
    assert stream.simulator.now.timestamp() == pytest.approx(clock.now, abs=1)
    assert stream.metrics.get(
        "windops_simulator_samples_generated_total",
        tenant_id="north-sea", turbine_id="dogger-east-t01",
    ) <= 6  # fmt: skip


def test_light_link_trouble_sends_broken_and_repeated_samples() -> None:
    """SIM-004 on the live stream: the Telemetry Service must see and record these."""
    transport, clock = FakeTransport(), Clock()
    stream = streamer(transport, clock, turbines=DEMO_TURBINES, problems="light")
    run(stream, clock, 900)
    samples = sent(transport)
    broken = [s for s in samples if isinstance(s, str) or "event_time" not in s]
    keys = [
        (s["turbine_id"], s["sequence"]) for s in samples if isinstance(s, dict) and "sequence" in s
    ]
    assert broken
    assert len(keys) > len(set(keys))  # duplicated


def test_tokens_are_read_from_the_mounted_secret_and_its_own_files_skipped(tmp_path: Path) -> None:
    (tmp_path / "north-sea.dogger-east-t01").write_text("abc\n")
    (tmp_path / "..data").mkdir()
    (tmp_path / "..2026_11_08").mkdir()
    (tmp_path / "empty.x").write_text("")
    assert load_tokens(tmp_path) == {("north-sea", "dogger-east-t01"): "abc"}
    assert load_tokens(tmp_path / "missing") == {}


def test_settings_refuse_plain_http_and_unknown_problems() -> None:
    with pytest.raises(ValueError, match="https"):
        Settings.from_environment({"WINDOPS_INGEST_URL": "http://gateway/ingest"})
    with pytest.raises(ValueError, match="DATA_PROBLEMS"):
        Settings.from_environment(
            {"WINDOPS_INGEST_URL": "https://g/i", "WINDOPS_SIMULATOR_DATA_PROBLEMS": "lots"}
        )
    ok = Settings.from_environment({"WINDOPS_INGEST_URL": "https://g/i", "WINDOPS_TLS_DIR": "/t"})
    assert ok.ca_file == Path("/t/ca.crt")


def test_it_serves_health_and_metrics() -> None:
    transport, clock = FakeTransport(), Clock()
    stream = streamer(transport, clock)
    server = health_server(stream, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/health/ready")  # noqa: S310  # A local test server.
        run(stream, clock, 6)
        assert urllib.request.urlopen(f"{base}/health/ready").status == 200  # noqa: S310
        assert urllib.request.urlopen(f"{base}/health/live").status == 200  # noqa: S310
        text = urllib.request.urlopen(f"{base}/metrics").read().decode()  # noqa: S310
    finally:
        server.shutdown()
    assert "# TYPE windops_simulator_samples_accepted_total counter" in text
    assert (
        'windops_simulator_last_accepted_timestamp_seconds{tenant_id="north-sea",turbine_id="dogger-east-t01"}'
        in text
    )


def test_metrics_keep_every_digit_of_a_timestamp() -> None:
    """A rounded timestamp would make every device look stale or fresh by minutes."""
    metrics = Metrics()
    metrics.set("windops_simulator_last_accepted_timestamp_seconds", 1794090675.25, turbine_id="t")
    assert 'windops_simulator_last_accepted_timestamp_seconds{turbine_id="t"} 1794090675.25' in (
        metrics.render()
    )


# --- CMS batches


def test_each_device_sends_a_cms_batch_on_every_ten_minute_boundary() -> None:
    transport, clock = FakeTransport(), Clock()
    clock.now = START - (START % 600) + 590  # 10 seconds before a boundary
    stream = streamer(transport, clock, turbines=TWO)
    run(stream, clock, 620)
    assert sorted(batch["turbine_id"] for batch in transport.cms) == [
        "atlas-ridge-t01", "atlas-ridge-t01", "dogger-east-t01", "dogger-east-t01",
    ]  # fmt: skip
    first = transport.cms[0]
    assert int(datetime.fromisoformat(first["event_time"]).timestamp()) % 600 == 0
    assert {window["sensor"] for window in first["sensors"]} == {
        "main_bearing", "gearbox", "generator",
    }  # fmt: skip
    assert len(first["sensors"][0]["values"]) == 8192
    assert first["synthetic"] is True


def test_a_device_flags_a_window_whose_level_jumps_and_keeps_its_baseline() -> None:
    device = Device(("north-sea", "dogger-east-t01"), "t")

    def window(level: float) -> dict[str, Any]:
        return {"sensors": [{"sensor": "gearbox", "values": [level, -level] * 512}]}

    assert [Streamer._trigger(device, window(0.05)) for _ in range(4)] == [None] * 4
    assert Streamer._trigger(device, window(0.2)) == "rms_above_baseline"
    assert device.baselines["gearbox"] == pytest.approx(0.05)
    assert Streamer._trigger(device, window(0.06)) is None


def test_cms_batches_wait_through_an_outage_and_keep_only_the_newest_hour() -> None:
    script: list[Answer | Exception] = [OSError("down")] * 10_000
    transport, clock = FakeTransport(script), Clock()
    clock.now = START - (START % 600)
    stream = streamer(transport, clock)

    def failing_cms(token: str, body: bytes, kind: str = "samples") -> Answer:
        raise OSError("down")

    transport.post = failing_cms  # type: ignore[method-assign]
    run(stream, clock, 600 * 8)
    device = stream.devices[("north-sea", "dogger-east-t01")]
    assert len(device.cms) == MAXIMUM_CMS_BUFFER
    assert stream.metrics.get(
        "windops_simulator_cms_windows_dropped_total", reason="buffer_full",
        tenant_id="north-sea", turbine_id="dogger-east-t01",
    ) == 3 * (8 - MAXIMUM_CMS_BUFFER)  # fmt: skip


def test_the_cms_route_sits_beside_the_samples_route() -> None:
    assert routes_for("https://gateway/ingest/v1/samples") == {
        "samples": "/ingest/v1/samples", "cms": "/ingest/v1/cms",
    }  # fmt: skip
    settings = Settings.from_environment(
        {"WINDOPS_INGEST_URL": "https://g/ingest/v1/samples", "WINDOPS_SIMULATOR_CMS_SECONDS": "0"}
    )
    assert settings.cms_seconds == 0
