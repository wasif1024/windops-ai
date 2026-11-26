// The production impact view's calls to Web BFF (M11.3). The Production Impact Service works out
// every figure, deterministically, with its range, inputs and assumptions (FR-IMP-001 to 003).
import { getJson, sendJson } from "../../api/client";

export interface ImpactInput {
  name: string;
  value: unknown;
  /** "request", "asset", "telemetry", "weather" or "reference". */
  source: string;
  source_ref: string;
  observed_at: string | null;
}

export interface Figure {
  name: string;
  value: number;
  low: number;
  high: number;
  unit: string;
}

export interface ImpactResult {
  result_id: string;
  case_id: string;
  turbine_id: string;
  wind_farm_id: string;
  action: string;
  downtime_hours: number;
  formula_version: string;
  inputs: ImpactInput[];
  figures: Figure[];
  assumptions: string[];
  window_start: string | null;
  window_end: string | null;
  content_hash: string;
  verified: boolean;
  estimated_by: string;
  estimated_at: string | null;
}

export interface EstimateIn {
  case_id: string;
  action: string;
  downtime_hours: number;
  planned_start?: string;
}

export function impactApi(token: string) {
  return {
    list: (caseId: string) => getJson<ImpactResult[]>(`/impact?case_id=${encodeURIComponent(caseId)}`, token),
    get: (id: string) => getJson<ImpactResult>(`/impact/${encodeURIComponent(id)}`, token),
    estimate: (key: string, body: EstimateIn) => sendJson<ImpactResult>("POST", "/impact", token, key, body),
  };
}

export type ImpactApi = ReturnType<typeof impactApi>;

/** "projected_lost_energy_mwh" into "Projected lost energy". */
export function figureName(name: string): string {
  const words = name.replace(/_(mwh|kwh|eur|usd|hours|pct|percent)$/, "").replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A figure in its unit, to a sensible precision. */
export function amount(value: number, unit: string): string {
  const rounded = Math.abs(value) >= 100 ? Math.round(value).toLocaleString() : (Math.round(value * 10) / 10).toLocaleString();
  return unit === "EUR" ? `€${rounded}` : `${rounded} ${unit}`;
}

/** Where the value sits in its range, from 0 to 100, for a bar. */
export function placeIn(figure: Figure): { low: number; value: number } {
  const span = figure.high - figure.low;
  if (!(span > 0) || figure.high <= 0) return { low: 0, value: 100 };
  return {
    low: Math.max(0, Math.min(100, (100 * figure.low) / figure.high)),
    value: Math.max(0, Math.min(100, (100 * figure.value) / figure.high)),
  };
}
