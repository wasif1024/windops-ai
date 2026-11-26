// The weather and source freshness panel's calls to Web BFF (M11.3). The Weather Service says
// how fresh its forecast is, and reports an outage rather than hiding it (WTH-005).
import { getJson } from "../../api/client";

export interface ForecastPoint {
  valid_at: string | null;
  wind_speed_hub_mps: number | null;
  wind_gusts_10m_mps: number | null;
  wind_speed_10m_mps: number | null;
  wind_direction_10m_deg: number | null;
  temperature_2m_c: number | null;
  precipitation_mm: number | null;
  visibility_m: number | null;
  cape_j_per_kg: number | null;
}

export interface Forecast {
  turbine_id: string;
  wind_farm_id: string;
  state: "fresh" | "stale" | "unavailable";
  note: string;
  age_seconds: number;
  provider: string;
  model: string;
  revision: string;
  forecast_id: string;
  retrieved_at: string | null;
  licence: string;
  from_time: string | null;
  to_time: string | null;
  summary: {
    max_wind_speed_hub_mps: number | null;
    max_wind_gusts_10m_mps: number | null;
    total_precipitation_mm: number | null;
    min_visibility_m: number | null;
    max_cape_j_per_kg: number | null;
    hours_covered: number;
    hours_wanted: number;
  };
  points: ForecastPoint[];
}

export function weatherApi(token: string) {
  return {
    forecast: (turbineId: string, hours: number) =>
      getJson<Forecast>(`/weather/${encodeURIComponent(turbineId)}?hours=${hours}`, token),
  };
}

export type WeatherApi = ReturnType<typeof weatherApi>;

export const STATE_STYLE: Record<Forecast["state"], string> = {
  fresh: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  stale: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  unavailable: "bg-red-50 text-red-800 ring-1 ring-red-200",
};

/** "3 h 5 min", from seconds. */
export function duration(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
}

export function value(n: number | null, unit: string, digits = 1): string {
  return n === null ? "—" : `${n.toFixed(digits)} ${unit}`;
}
