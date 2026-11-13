"""Evaluate fault rules against a turbine's recent telemetry and alarms. Pure, and deterministic.

Given the same window, a rule always gives the same finding, so a detection can be reproduced
from its snapshot (FR-FLT-003). Nothing here calls out, reads a clock or guesses: what a rule did
not see, it does not find.
"""

import math
import statistics
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from windops_fault_detection.rules import Rule

# How much of a held period must have samples, so a gap is never read as a reading held.
COVERAGE = 0.9


@dataclass(frozen=True)
class Sample:
    at: datetime
    # Each reading, or None while its sensor sent nothing.
    readings: Mapping[str, float | None]


@dataclass(frozen=True)
class AlarmView:
    code: str
    tag: str
    standing: bool
    raised_at: datetime
    cleared_at: datetime | None = None
    alarm_id: str = ""


@dataclass(frozen=True)
class Window:
    """What a rule may look at: one turbine's samples in time order, and its alarms."""

    turbine_id: str
    rated_power_kw: float
    rotor_diameter_m: float
    end: datetime
    samples: Sequence[Sample]
    alarms: Sequence[AlarmView] = ()


@dataclass(frozen=True)
class Finding:
    rule_id: str
    version: int
    kind: str
    severity: str
    # The figure the severity came from, in the rule's own units.
    amount: float
    summary: str
    # Every value the finding rests on, for its record.
    observed: Mapping[str, Any] = field(default_factory=dict)
    since: datetime | None = None


def _series(samples: Iterable[Sample], metric: str) -> list[tuple[datetime, float]]:
    return [(s.at, float(v)) for s in samples if (v := s.readings.get(metric)) is not None]


def _held(points: Sequence[tuple[datetime, float]], end: datetime, seconds: int) -> list[float]:
    """The values in the last `seconds`, or [] if too few samples cover it."""
    start = end - timedelta(seconds=seconds)
    values = [value for at, value in points if start < at <= end]
    return values if len(values) >= COVERAGE * seconds else []


def _rolling_mean(
    points: Sequence[tuple[datetime, float]], seconds: int
) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    window: deque[tuple[datetime, float]] = deque()
    total = 0.0
    for at, value in points:
        window.append((at, value))
        total += value
        while window[0][0] <= at - timedelta(seconds=seconds):
            total -= window.popleft()[1]
        out.append((at, total / len(window)))
    return out


def _sustained(rule: Rule, held: Sequence[float]) -> str | None:
    """The severity of the highest level every held value reached."""
    return rule.severity_for(min(held)) if held else None


def threshold(rule: Rule, window: Window) -> Finding | None:
    p = rule.parameters
    points = _series(window.samples, p["metric"])
    if "mean_seconds" in p:
        points = _rolling_mean(points, p["mean_seconds"])
    held = _held(points, window.end, p["for_seconds"])
    severity = _sustained(rule, held)
    if severity is None:
        return None
    least = min(held)
    return Finding(
        rule.rule_id, rule.version, rule.kind, severity, least,
        f"{p['metric']} stayed at or above {least:.2f} for {p['for_seconds']} seconds",
        {"metric": p["metric"], "least": round(least, 3), "latest": round(held[-1], 3),
         "for_seconds": p["for_seconds"], "mean_seconds": p.get("mean_seconds", 0)},
        window.end - timedelta(seconds=p["for_seconds"]),
    )  # fmt: skip


def expected_temperatures(rule: Rule, window: Window) -> list[tuple[datetime, float, float]]:
    """(time, measured, expected) for each sample with the part's reading, power and air.

    The expected temperature follows the part's thermal target, set by load squared, as its
    datasheet gives it, through its time constant. It starts at the first target, and is used
    only after two time constants, by when where it started no longer matters.
    """
    p = rule.parameters
    tau, rated = p["time_constant_s"], window.rated_power_kw
    out: list[tuple[datetime, float, float]] = []
    expected: float | None = None
    last: datetime | None = None
    warm_from: datetime | None = None
    for sample in window.samples:
        measured = sample.readings.get(p["metric"])
        power, air = sample.readings.get("power_kw"), sample.readings.get("ambient_temp_c")
        if power is None or air is None or rated <= 0:
            continue
        load = min(max(power / rated, 0.0), 1.0)
        target = (air + p["nacelle_above_ambient_c"] + p["rise_at_idle_c"]
                  + (p["rise_at_full_load_c"] - p["rise_at_idle_c"]) * load**2)  # fmt: skip
        if expected is None or last is None:
            expected, warm_from = target, sample.at + timedelta(seconds=2 * tau)
        else:
            step = (sample.at - last).total_seconds()
            expected = target + (expected - target) * math.exp(-step / tau)
        last = sample.at
        if measured is not None and warm_from is not None and sample.at >= warm_from:
            out.append((sample.at, float(measured), expected))
    return out


def expected_by_speed(rule: Rule, window: Window) -> list[tuple[datetime, float, float]]:
    """(time, measured, expected) for each sample with the reading and rotor speed: a healthy
    rise with the rotor's speed as a fraction of its maximum, which its tip speed limits."""
    p = rule.parameters
    radius = window.rotor_diameter_m / 2
    if radius <= 0:
        return []
    maximum_rpm = p["maximum_tip_speed_mps"] / radius * 60 / (2 * math.pi)
    out: list[tuple[datetime, float, float]] = []
    for sample in window.samples:
        measured, rpm = sample.readings.get(p["metric"]), sample.readings.get("rotor_speed_rpm")
        if measured is None or rpm is None:
            continue
        fraction = min(max(rpm / maximum_rpm, 0.0), 1.0)
        expected = p["at_standstill"] + p["rise_at_full_speed"] * fraction ** p["exponent"]
        out.append((sample.at, float(measured), expected))
    return out


def residual(rule: Rule, window: Window) -> Finding | None:
    p = rule.parameters
    speed = p.get("model", "thermal") == "speed"
    modelled = expected_by_speed(rule, window) if speed else expected_temperatures(rule, window)
    gaps = [(at, measured - expected) for at, measured, expected in modelled]
    if "mean_seconds" in p:
        gaps = _rolling_mean(gaps, p["mean_seconds"])
    held = _held(gaps, window.end, p["for_seconds"])
    severity = _sustained(rule, held)
    if severity is None:
        return None
    _, measured, expected = modelled[-1]
    least = min(held)
    unit = "" if speed else " °C"
    return Finding(
        rule.rule_id, rule.version, rule.kind, severity, least,
        f"{p['metric']} ran at least {least:.2f}{unit} above what its normal behaviour model "
        f"explains, for {p['for_seconds']} seconds",
        {"metric": p["metric"], "model": p.get("model", "thermal"),
         "least_residual": round(least, 3), "measured": round(measured, 3),
         "expected": round(expected, 3), "for_seconds": p["for_seconds"],
         "mean_seconds": p.get("mean_seconds", 0)},
        window.end - timedelta(seconds=p["for_seconds"]),
    )  # fmt: skip


def _minute_means(points: Sequence[tuple[datetime, float]]) -> list[float]:
    buckets: dict[int, list[float]] = {}
    for at, value in points:
        buckets.setdefault(int(at.timestamp()) // 60, []).append(value)
    return [statistics.fmean(values) for _, values in sorted(buckets.items())]


def rolling_zscore(rule: Rule, window: Window) -> Finding | None:
    p = rule.parameters
    points = _series(window.samples, p["metric"])
    recent_from = window.end - timedelta(seconds=p["recent_seconds"])
    baseline_from = recent_from - timedelta(seconds=p["baseline_seconds"])
    recent = _held(points, window.end, p["recent_seconds"])
    baseline = _minute_means([(at, v) for at, v in points if baseline_from < at <= recent_from])
    if not recent or len(baseline) < COVERAGE * p["baseline_seconds"] / 60:
        return None
    spread = max(statistics.pstdev(baseline), p["minimum_spread"])
    if spread <= 0:
        return None
    score = (statistics.fmean(recent) - statistics.fmean(baseline)) / spread
    severity = rule.severity_for(score)
    if severity is None:
        return None
    return Finding(
        rule.rule_id, rule.version, rule.kind, severity, score,
        f"{p['metric']}'s last {p['recent_seconds']} seconds sit {score:.1f} spreads above its "
        "baseline",
        {"metric": p["metric"], "zscore": round(score, 3),
         "recent_mean": round(statistics.fmean(recent), 3),
         "baseline_mean": round(statistics.fmean(baseline), 3), "spread": round(spread, 3)},
        recent_from,
    )  # fmt: skip


def alarm_correlation(rule: Rule, window: Window, found: Mapping[str, Finding]) -> Finding | None:
    p = rule.parameters
    since = window.end - timedelta(seconds=p["within_seconds"])
    recent = [
        alarm for alarm in window.alarms
        if alarm.standing or (alarm.cleared_at is not None and alarm.cleared_at >= since)
    ]  # fmt: skip
    seen = {alarm.code for alarm in recent}
    codes = set(p["alarm_codes"])
    also = p.get("with_rule", "")
    if not codes <= seen or (also and also not in found):
        return None
    severity = rule.severity_for(1.0)
    if severity is None:
        return None
    parts = sorted(codes) + ([f"rule {also}"] if also else [])
    return Finding(
        rule.rule_id, rule.version, rule.kind, severity, 1.0,
        "Together within the window: " + ", ".join(parts),
        {"alarm_codes": sorted(codes), "with_rule": also,
         "alarm_ids": sorted(a.alarm_id for a in recent if a.code in codes and a.alarm_id)},
        since,
    )  # fmt: skip


def data_gap(rule: Rule, window: Window) -> Finding | None:
    p = rule.parameters
    start = window.end - timedelta(seconds=p["window_seconds"])
    inside = [s for s in window.samples if start < s.at <= window.end]
    present = sum(1 for s in inside if s.readings.get(p["metric"]) is not None)
    # One sample a second: what never arrived is missing too.
    missing = 100.0 * (1 - present / p["window_seconds"])
    severity = rule.severity_for(missing)
    if severity is None:
        return None
    return Finding(
        rule.rule_id, rule.version, rule.kind, severity, missing,
        f"{missing:.0f}% of {p['metric']} missing over {p['window_seconds']} seconds",
        {"metric": p["metric"], "missing_percent": round(missing, 2), "present": present,
         "window_seconds": p["window_seconds"]}, start,
    )  # fmt: skip


_PLAIN = {
    "threshold": threshold, "residual": residual, "rolling_zscore": rolling_zscore,
    "data_gap": data_gap,
}  # fmt: skip


def evaluate(rules: Iterable[Rule], window: Window) -> list[Finding]:
    """Every rule's finding, if any. A correlation may name another rule, so those come last."""
    found: dict[str, Finding] = {}
    ordered = sorted(rules, key=lambda rule: (rule.kind == "alarm_correlation", rule.rule_id))
    for rule in ordered:
        if rule.kind == "alarm_correlation":
            finding = alarm_correlation(rule, window, found)
        else:
            finding = _PLAIN[rule.kind](rule, window)
        if finding is not None:
            found[rule.rule_id] = finding
    return list(found.values())


def lookback(rules: Iterable[Rule]) -> timedelta:
    """How much telemetry the rules need, so a caller fetches no more and no less."""
    needed = 60
    for rule in rules:
        p = rule.parameters
        needed = max(
            needed,
            {
                "threshold": p.get("for_seconds", 0) + p.get("mean_seconds", 0),
                "residual": 2 * p.get("time_constant_s", 0)
                + p.get("for_seconds", 0)
                + p.get("mean_seconds", 0),
                "rolling_zscore": p.get("recent_seconds", 0) + p.get("baseline_seconds", 0),
                "data_gap": p.get("window_seconds", 0),
            }.get(rule.kind, 0),
        )
    return timedelta(seconds=int(needed))


def readings_needed(rules: Iterable[Rule]) -> list[str]:
    """The readings the rules look at, and what their models need, so no more is fetched."""
    needed: set[str] = set()
    for rule in rules:
        p = rule.parameters
        if "metric" in p:
            needed.add(p["metric"])
        if rule.kind == "residual":
            needed |= {"rotor_speed_rpm"} if p.get("model") == "speed" else {
                "power_kw", "ambient_temp_c",
            }  # fmt: skip
    return sorted(needed)
