"""The rules each tenant starts with, version 1 of each, until a Tenant Administrator changes them.

The normal behaviour models use each part's datasheet: its temperature rise above the nacelle at
idle and at full load, and its thermal time constant. The demo turbines are synthetic, so these are
the simulator's own figures. Each level was tuned against the simulator: a day of the healthy fleet
finds nothing, each fault scenario is found, and the high-load false positive is not.
"""

from typing import Any

LIBRARY = "fault-rules-1"


def _residual(metric: str, idle: float, full: float, tau: int, levels: list[Any]) -> dict[str, Any]:
    return {
        "kind": "residual",
        "parameters": {
            "metric": metric, "rise_at_idle_c": idle, "rise_at_full_load_c": full,
            "time_constant_s": tau, "nacelle_above_ambient_c": 8.0, "for_seconds": 600,
        },
        "levels": levels,
    }  # fmt: skip


def _gap(metric: str) -> dict[str, Any]:
    return {
        "kind": "data_gap",
        "parameters": {"metric": metric, "window_seconds": 600},
        "levels": [{"at": 10, "severity": "low"}, {"at": 30, "severity": "medium"}],
    }


# (rule ID, name, description, definition)
DEFAULT_RULES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("gearbox-oil-residual", "Gearbox oil hotter than its load explains",
     "Failing cooling or lubrication, or a drifting sensor",
     _residual("gearbox_oil_temp_c", 25.0, 45.0, 1800, [
         {"at": 4, "severity": "medium"}, {"at": 8, "severity": "high"},
         {"at": 15, "severity": "critical"}])),
    ("gearbox-bearing-residual", "Gearbox bearing hotter than its load explains",
     "Bearing wear, or heat from the gearbox oil",
     _residual("gearbox_bearing_temp_c", 28.0, 55.0, 1200, [
         {"at": 4, "severity": "medium"}, {"at": 8, "severity": "high"}])),
    ("main-bearing-residual", "Main bearing hotter than its load explains",
     "Main bearing wear or lubrication",
     _residual("main_bearing_temp_c", 10.0, 22.0, 2400, [
         {"at": 3, "severity": "medium"}, {"at": 6, "severity": "high"}])),
    ("generator-bearing-residual", "Generator bearing hotter than its load explains",
     "Generator bearing wear or lubrication",
     _residual("generator_bearing_temp_c", 22.0, 45.0, 1500, [
         {"at": 4, "severity": "medium"}, {"at": 8, "severity": "high"}])),
    ("gearbox-oil-critical", "Gearbox oil above its safe limit",
     "A safety limit, whatever the load",
     {"kind": "threshold",
      "parameters": {"metric": "gearbox_oil_temp_c", "mean_seconds": 60, "for_seconds": 60},
      "levels": [{"at": 75, "severity": "critical"}]}),
    ("vibration-residual", "Nacelle vibration above what its rotor speed explains",
     "Imbalance, bearing wear or a loose part",
     {"kind": "residual",
      "parameters": {"metric": "nacelle_vibration_mm_s", "model": "speed", "at_standstill": 0.15,
                     "rise_at_full_speed": 1.6, "exponent": 1.5, "maximum_tip_speed_mps": 90.0,
                     "mean_seconds": 600, "for_seconds": 600},
      "levels": [{"at": 0.4, "severity": "medium"}, {"at": 0.8, "severity": "high"}]}),
    ("vibration-rise", "Nacelle vibration rising above its own baseline",
     "A sudden change, whatever its cause",
     {"kind": "rolling_zscore",
      "parameters": {"metric": "nacelle_vibration_mm_s", "recent_seconds": 600,
                     "baseline_seconds": 7200, "minimum_spread": 0.15},
      "levels": [{"at": 4, "severity": "medium"}, {"at": 8, "severity": "high"}]}),
    ("vibration-high", "Nacelle vibration high for ten minutes",
     "Above where a healthy nacelle runs (ISO 10816-21)",
     {"kind": "threshold",
      "parameters": {"metric": "nacelle_vibration_mm_s", "mean_seconds": 600, "for_seconds": 300},
      "levels": [{"at": 2.8, "severity": "medium"}, {"at": 3.5, "severity": "high"}]}),
    ("gearbox-oil-alarm-and-residual", "Gearbox oil alarm, and hotter than its load explains",
     "The controller's alarm, confirmed by the normal behaviour model",
     {"kind": "alarm_correlation",
      "parameters": {"alarm_codes": ["GBX_OIL_TEMP_HIGH"], "with_rule": "gearbox-oil-residual",
                     "within_seconds": 3600},
      "levels": [{"at": 1, "severity": "high"}]}),
    ("gearbox-oil-gap", "Gearbox oil temperature missing", "A sensor or its wiring",
     _gap("gearbox_oil_temp_c")),
    ("main-bearing-gap", "Main bearing temperature missing", "A sensor or its wiring",
     _gap("main_bearing_temp_c")),
    ("vibration-gap", "Nacelle vibration missing", "A sensor or its wiring",
     _gap("nacelle_vibration_mm_s")),
)  # fmt: skip
