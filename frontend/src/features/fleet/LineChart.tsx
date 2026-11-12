import { useId } from "react";
import type { Series } from "./api";
import { formatValue, labelOf } from "./format";

export const WIDTH = 600;
export const HEIGHT = 160;

export interface Drawn {
  /** SVG path data, one "M…L…" run between missing readings. */
  path: string;
  /** Where each suspect reading sits, to mark it. */
  suspect: { x: number; y: number }[];
  minimum: number | null;
  maximum: number | null;
  latest: number | null;
  missing: number;
}

/**
 * Lay a series out on a WIDTH × HEIGHT canvas, by time across and value up. A missing reading
 * breaks the line, so a gap shows as a gap, never as a straight line drawn over it.
 */
export function draw(series: Series, from: string, to: string, width = WIDTH, height = HEIGHT): Drawn {
  const start = Date.parse(from);
  const span = Math.max(1, Date.parse(to) - start);
  const values = series.points.flatMap((point) => (point.value === null ? [] : [point.value]));
  const missing = series.points.length - values.length;
  if (values.length === 0) return { path: "", suspect: [], minimum: null, maximum: null, latest: null, missing };
  const low = Math.min(...values);
  const high = Math.max(...values);
  // A flat line sits in the middle, rather than on the floor.
  const pad = high === low ? Math.max(1, Math.abs(high) * 0.05) : (high - low) * 0.08;
  const bottom = low - pad;
  const range = high + pad - bottom;
  const x = (time: string) => ((Date.parse(time) - start) / span) * width;
  const y = (value: number) => height - ((value - bottom) / range) * height;
  let path = "";
  let drawing = false;
  const suspect: { x: number; y: number }[] = [];
  for (const point of series.points) {
    if (point.value === null) {
      drawing = false;
      continue;
    }
    const px = x(point.time).toFixed(1);
    const py = y(point.value).toFixed(1);
    path += `${drawing ? "L" : "M"}${px},${py}`;
    drawing = true;
    if (point.quality === "suspect") suspect.push({ x: Number(px), y: Number(py) });
  }
  return { path, suspect, minimum: low, maximum: high, latest: values[values.length - 1] ?? null, missing };
}

function clock(time: string): string {
  return new Date(time).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** One metric over the window: the line, its range, and suspect and missing readings marked. */
export function LineChart({ series, from, to }: { series: Series; from: string; to: string }) {
  const title = useId();
  const drawn = draw(series, from, to);
  const label = labelOf(series.metric);
  const summary =
    drawn.latest === null
      ? `${label}: no readings in this window.`
      : `${label}: latest ${formatValue(drawn.latest, series.unit)}, from ${formatValue(drawn.minimum, series.unit)} ` +
        `to ${formatValue(drawn.maximum, series.unit)}. ${drawn.suspect.length} suspect and ${drawn.missing} missing readings.`;
  return (
    <figure className="min-w-0" aria-labelledby={title}>
      <figcaption id={title} className="flex flex-wrap items-baseline justify-between gap-x-3 text-sm">
        <span className="font-medium text-slate-800">{label}</span>
        <span className="text-slate-600" data-testid={`latest-${series.metric}`}>
          {formatValue(drawn.latest, series.unit)}
        </span>
      </figcaption>
      <p className="sr-only">{summary}</p>
      <div className="relative mt-1 rounded-md border border-slate-200 bg-slate-50">
        {drawn.latest === null ? (
          <p className="flex h-40 items-center justify-center text-sm text-slate-500">No readings in this window</p>
        ) : (
          <>
            <svg
              viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
              preserveAspectRatio="none"
              className="block h-40 w-full"
              aria-hidden="true"
              data-testid={`chart-${series.metric}`}
            >
              <path d={drawn.path} fill="none" stroke="var(--brand-primary, #0b5394)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
              {drawn.suspect.map((point) => (
                <circle key={`${point.x},${point.y}`} cx={point.x} cy={point.y} r="3" fill="#d97706" vectorEffect="non-scaling-stroke" />
              ))}
            </svg>
            <span className="absolute left-2 top-1 text-xs text-slate-500">{formatValue(drawn.maximum, series.unit)}</span>
            <span className="absolute bottom-1 left-2 text-xs text-slate-500">{formatValue(drawn.minimum, series.unit)}</span>
          </>
        )}
      </div>
      <div className="mt-1 flex justify-between text-xs text-slate-500" aria-hidden="true">
        <span>{clock(from)}</span>
        {(drawn.suspect.length > 0 || drawn.missing > 0) && (
          <span className="text-amber-800">
            {drawn.suspect.length > 0 && `${drawn.suspect.length} suspect`}
            {drawn.suspect.length > 0 && drawn.missing > 0 && ", "}
            {drawn.missing > 0 && `${drawn.missing} missing`}
          </span>
        )}
        <span>{clock(to)}</span>
      </div>
    </figure>
  );
}
