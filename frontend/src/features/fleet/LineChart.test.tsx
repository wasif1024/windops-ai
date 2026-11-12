import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Series } from "./api";
import { draw, HEIGHT, LineChart, WIDTH } from "./LineChart";

const FROM = "2026-11-12T12:00:00Z";
const TO = "2026-11-12T12:00:04Z";

function series(values: (number | null)[], suspect: number[] = []): Series {
  return {
    metric: "power",
    unit: "kW",
    points: values.map((value, index) => ({
      time: new Date(Date.parse(FROM) + index * 1000).toISOString(),
      value,
      quality: value === null ? "missing" : suspect.includes(index) ? "suspect" : "good",
    })),
  };
}

afterEach(cleanup);

describe("the line chart", () => {
  it("spans the window by time, and breaks the line at a missing reading", () => {
    const drawn = draw(series([10, 20, null, 30, 40], [3]), FROM, TO);
    const runs = drawn.path.split("M").filter(Boolean);
    expect(runs).toHaveLength(2); // before and after the gap
    expect(drawn.path.startsWith("M0.0,")).toBe(true);
    expect(drawn.path).toContain(`L${WIDTH.toFixed(1)},`); // the last point at the window's end
    expect(drawn.suspect).toHaveLength(1);
    expect([drawn.minimum, drawn.maximum, drawn.latest, drawn.missing]).toEqual([10, 40, 40, 1]);
  });

  it("marks only suspect readings, not ones still waiting to be checked", () => {
    const marked = series([10, 20, 30], [1]);
    const waiting = { ...marked, points: marked.points.map((p, i) => (i === 1 ? p : { ...p, quality: "unchecked" })) };
    expect(draw(waiting, FROM, TO).suspect).toHaveLength(1);
  });

  it("keeps every point inside the canvas, a flat line in the middle", () => {
    const flat = draw(series([5, 5, 5]), FROM, TO);
    const ys = [...flat.path.matchAll(/[ML][\d.]+,([\d.]+)/g)].map((match) => Number(match[1]));
    expect(ys.every((y) => y === HEIGHT / 2)).toBe(true);
    const wide = draw(series([-50, 0, 1000]), FROM, TO);
    for (const [, y] of wide.path.matchAll(/[ML][\d.]+,([\d.]+)/g)) {
      expect(Number(y)).toBeGreaterThanOrEqual(0);
      expect(Number(y)).toBeLessThanOrEqual(HEIGHT);
    }
  });

  it("says what it shows to a screen reader, and when there is nothing", () => {
    render(<LineChart series={series([10, 20], [1])} from={FROM} to={TO} />);
    expect(screen.getByText(/Power: latest 20\.0 kW, from 10\.0 kW to 20\.0 kW\. 1 suspect and 0 missing/)).toBeTruthy();
    cleanup();
    render(<LineChart series={series([null])} from={FROM} to={TO} />);
    expect(screen.getByText("No readings in this window")).toBeTruthy();
  });
});
