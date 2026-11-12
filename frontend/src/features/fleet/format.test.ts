import { describe, expect, it } from "vitest";
import type { Latest } from "./api";
import { ageText, formatValue, freshness, labelOf, titleCase } from "./format";

const NOW = Date.parse("2026-11-12T12:00:00Z");

function latest(secondsAgo: number): Latest {
  return {
    event_time: new Date(NOW - secondsAgo * 1000).toISOString(),
    ingested_at: null,
    delay_seconds: 0,
    operating_state: "producing",
    available: true,
    readings: [],
  };
}

describe("fleet formatting", () => {
  it("calls a sample live for a minute, delayed for five, then stale", () => {
    expect(freshness(latest(5), NOW)).toBe("live");
    expect(freshness(latest(120), NOW)).toBe("delayed");
    expect(freshness(latest(600), NOW)).toBe("stale");
    expect(freshness(null, NOW)).toBe("none");
    expect(ageText(latest(45), NOW)).toBe("45 s ago");
    expect(ageText(latest(600), NOW)).toBe("10 min ago");
    expect(ageText(null, NOW)).toBe("No data in the last day");
  });

  it("shows values with their units, and a dash for a missing reading", () => {
    expect(formatValue(7234.6, "kW")).toMatch(/^7,?235 kW$/);
    expect(formatValue(61.27, "degC")).toBe("61.3 °C");
    expect(formatValue(270, "deg")).toBe("270°");
    expect(formatValue(null, "kW")).toBe("—");
  });

  it("names metrics and statuses for people", () => {
    expect(labelOf("gearbox_temperature")).toBe("Gearbox oil temperature");
    expect(labelOf("something_new")).toBe("something_new");
    expect(titleCase("in_service")).toBe("In service");
  });
});
