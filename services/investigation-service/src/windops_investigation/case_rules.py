"""The case rules: how detections group into cases, so one fault makes one case (FR-FLT-004).

Each fault rule belongs to a component group. A new detection joins the turbine's active case for
its group, however long that case has been open, as it is the one investigation of that component;
otherwise it opens a new case. Only a detection at OPENS_AT or above opens a case: a low one, such
as a short data gap, joins an active case but never starts one. Versioned, and each case records
the version that grouped it.
"""

VERSION = "case-rules-1"
SEVERITIES = ("low", "medium", "high", "critical")
OPENS_AT = "medium"
# A tenant's own rules, and a person's cases, are about the turbine as a whole.
DEFAULT_GROUP = "turbine"
GROUPS = {
    "gearbox-oil-residual": "gearbox",
    "gearbox-bearing-residual": "gearbox",
    "gearbox-oil-critical": "gearbox",
    "gearbox-oil-alarm-and-residual": "gearbox",
    "gearbox-oil-gap": "sensors",
    "main-bearing-residual": "main_bearing",
    "main-bearing-gap": "sensors",
    "generator-bearing-residual": "generator",
    "vibration-residual": "vibration",
    "vibration-rise": "vibration",
    "vibration-high": "vibration",
    "vibration-gap": "sensors",
}


def group_of(rule_id: str) -> str:
    return GROUPS.get(rule_id, DEFAULT_GROUP)


def rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else -1


def opens_a_case(severity: str) -> bool:
    return rank(severity) >= rank(OPENS_AT)


def highest(*severities: str) -> str:
    return max(severities, key=rank)
