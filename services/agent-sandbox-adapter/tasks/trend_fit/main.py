"""A least-squares straight line through readings over time. Reads JSON on stdin, prints JSON.

Standard library only: the sandbox installs nothing.
"""

import json
import sys


def main():
    given = json.load(sys.stdin)
    hours = [float(p["hours"]) for p in given["points"]]
    values = [float(p["value"]) for p in given["points"]]
    count = len(hours)
    mean_h, mean_v = sum(hours) / count, sum(values) / count
    spread = sum((h - mean_h) ** 2 for h in hours)
    if spread == 0:
        slope = 0.0
    else:
        pairs = zip(hours, values, strict=True)
        slope = sum((h - mean_h) * (v - mean_v) for h, v in pairs) / spread
    intercept = mean_v - slope * mean_h
    total = sum((v - mean_v) ** 2 for v in values)
    residual = sum((v - (intercept + slope * h)) ** 2 for h, v in zip(hours, values, strict=True))
    r_squared = 1.0 if total == 0 else max(0.0, 1 - residual / total)
    answer = {
        "slope_per_hour": round(slope, 6),
        "intercept": round(intercept, 6),
        "r_squared": round(r_squared, 6),
        "points": count,
    }
    if "threshold" in given:
        last = max(hours)
        now = intercept + slope * last
        gap = float(given["threshold"]) - now
        if gap == 0:
            answer["hours_to_threshold"] = 0.0
        elif slope == 0 or (gap > 0) != (slope > 0):
            answer["hours_to_threshold"] = None
        else:
            answer["hours_to_threshold"] = round(gap / slope, 3)
    print(json.dumps(answer))


main()
