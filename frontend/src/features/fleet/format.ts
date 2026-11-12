// How the fleet screens name and show readings, statuses and freshness.
import type { Latest, Reading } from "./api";

/** Each metric's label, in the order a turbine's readings are shown. */
export const METRICS: { id: string; label: string }[] = [
  { id: "power", label: "Power" },
  { id: "wind_speed", label: "Wind speed" },
  { id: "wind_direction", label: "Wind direction" },
  { id: "rotor_speed", label: "Rotor speed" },
  { id: "generator_speed", label: "Generator speed" },
  { id: "pitch_angle", label: "Pitch angle" },
  { id: "ambient_temperature", label: "Ambient temperature" },
  { id: "gearbox_temperature", label: "Gearbox oil temperature" },
  { id: "bearing_temperature", label: "Gearbox bearing temperature" },
  { id: "main_bearing_temperature", label: "Main bearing temperature" },
  { id: "generator_bearing_temperature", label: "Generator bearing temperature" },
  { id: "vibration_rms", label: "Nacelle vibration" },
];

export function labelOf(metric: string): string {
  return METRICS.find((item) => item.id === metric)?.label ?? metric;
}

const UNITS: Record<string, string> = { degC: "°C", deg: "°" };

export function unitOf(unit: string): string {
  return UNITS[unit] ?? unit;
}

/** A value with its unit, to a sensible precision, or a dash when the sensor sent nothing. */
export function formatValue(value: number | null, unit: string): string {
  if (value === null) return "—";
  const digits = Math.abs(value) >= 100 ? 0 : 1;
  const text = value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
  const shown = unitOf(unit);
  return shown === "°" ? `${text}°` : `${text} ${shown}`;
}

export function reading(latest: Latest | null, metric: string): Reading | undefined {
  return latest?.readings.find((item) => item.metric === metric);
}

// Asset statuses, from best to worst, each with its badge's colours.
const STATUS_STYLE: Record<string, string> = {
  operating: "bg-emerald-100 text-emerald-900",
  curtailed: "bg-sky-100 text-sky-900",
  maintenance: "bg-amber-100 text-amber-900",
  stopped: "bg-slate-200 text-slate-800",
  offline: "bg-slate-200 text-slate-800",
  fault: "bg-red-100 text-red-900",
};

export function statusStyle(status: string): string {
  return STATUS_STYLE[status] ?? "bg-slate-200 text-slate-800";
}

export function titleCase(text: string): string {
  const spaced = text.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export type Freshness = "live" | "delayed" | "stale" | "none";

// A device sends every few seconds, so a minute of silence is late, and five is stale.
export const LIVE_SECONDS = 60;
export const STALE_SECONDS = 300;

/** How fresh a turbine's newest sample is, now. */
export function freshness(latest: Latest | null, now: number = Date.now()): Freshness {
  if (!latest?.event_time) return "none";
  const age = (now - Date.parse(latest.event_time)) / 1000;
  if (age <= LIVE_SECONDS) return "live";
  return age <= STALE_SECONDS ? "delayed" : "stale";
}

export function ageText(latest: Latest | null, now: number = Date.now()): string {
  if (!latest?.event_time) return "No data in the last day";
  const seconds = Math.max(0, Math.round((now - Date.parse(latest.event_time)) / 1000));
  if (seconds < 60) return `${seconds} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export const FRESHNESS_STYLE: Record<Freshness, { label: string; dot: string }> = {
  live: { label: "Live", dot: "bg-emerald-500" },
  delayed: { label: "Delayed", dot: "bg-amber-500" },
  stale: { label: "Stale", dot: "bg-red-500" },
  none: { label: "No data", dot: "bg-slate-400" },
};
