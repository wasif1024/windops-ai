"""The rules each tenant starts with, version 1 of each, until a Tenant Administrator changes them.

These are starting values, not a site's approved figures: each tenant's administrators set their
own, and each site's, as new versions (FR-POL-001). The wind limits are the common climbing limits
for turbine technicians; the rest are business defaults for review.
"""

from typing import Any

LIBRARY = "policy-rules-1"
ACTIVE_CASE = ["open", "investigating", "pending_review"]
CLIMB = {"input": "proposal.requires_climb", "equals": True}
# A forecast older than this is stale for a decision about work at height (WTH-005).
FORECAST_AGE_SECONDS = 3 * 3600

# (rule ID, name, description, definition)
DEFAULT_RULES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("work-at-height-wind", "Wind too strong to climb",
     "Work at height only while the forecast wind over the planned window stays at or below the "
     "climbing limit",
     {"kind": "limit", "category": "weather_window",
      "parameters": {"input": "weather.max_wind_speed_mps", "above": 12.0, "when": CLIMB,
                     "max_age_seconds": FORECAST_AGE_SECONDS},
      "outcome": "block", "when_missing": "block"}),
    ("work-at-height-gusts", "Gusts too strong to climb",
     "Work at height only while forecast gusts over the planned window stay at or below the limit",
     {"kind": "limit", "category": "weather_window",
      "parameters": {"input": "weather.max_gust_mps", "above": 18.0, "when": CLIMB,
                     "max_age_seconds": FORECAST_AGE_SECONDS},
      "outcome": "block", "when_missing": "block"}),
    ("evidence-ready", "Evidence frozen",
     "A recommendation must rest on the case's frozen evidence",
     {"kind": "match", "category": "evidence",
      "parameters": {"input": "evidence.status", "not_in": ["ready"]},
      "outcome": "block", "when_missing": "block"}),
    ("evidence-fresh", "Evidence recent enough",
     "Evidence older than a day may no longer describe the turbine",
     {"kind": "limit", "category": "evidence",
      "parameters": {"input": "evidence.age_hours", "above": 24.0},
      "outcome": "block", "when_missing": "block"}),
    ("case-active", "Case still open",
     "Nothing is recommended on a closed or cancelled case",
     {"kind": "match", "category": "escalation",
      "parameters": {"input": "case.status", "not_in": ACTIVE_CASE},
      "outcome": "block", "when_missing": "block"}),
    ("critical-case", "Critical cases go to a person",
     "A critical case is decided by a person, whatever the recommendation",
     {"kind": "match", "category": "risk",
      "parameters": {"input": "case.severity", "in": ["critical"]},
      "outcome": "escalate", "when_missing": "escalate"}),
    ("turbine-offline", "Turbine out of contact",
     "A turbine that is offline cannot confirm its state before work starts",
     {"kind": "match", "category": "safety",
      "parameters": {"input": "turbine.status", "in": ["offline"]},
      "outcome": "escalate", "when_missing": "escalate"}),
    ("low-confidence", "Confidence too low",
     "A recommendation stated with low confidence goes to a person",
     {"kind": "limit", "category": "risk",
      "parameters": {"input": "proposal.confidence", "below": 0.6},
      "outcome": "escalate", "when_missing": "escalate"}),
    ("cost-limit", "Cost above the approval limit",
     "Spending above the limit needs a person's decision",
     {"kind": "limit", "category": "business",
      "parameters": {"input": "proposal.estimated_cost_eur", "above": 50000.0},
      "outcome": "escalate", "when_missing": "escalate"}),
    ("downtime-limit", "Downtime above the limit",
     "Taking a turbine down for longer than the limit needs a person's decision",
     {"kind": "limit", "category": "business",
      "parameters": {"input": "proposal.estimated_downtime_hours", "above": 48.0},
      "outcome": "escalate", "when_missing": "escalate"}),
)  # fmt: skip
