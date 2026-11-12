// The fleet screens' calls to Web BFF (M4.8). Read only: the Asset and Telemetry services decide
// what each user sees, as the BFF asks them as the signed-in user.
import { getJson } from "../../api/client";

export interface Reading {
  metric: string;
  /** null when the sensor sent nothing. */
  value: number | null;
  unit: string;
  /** "good", "suspect", "unchecked" (not yet checked) or "missing". */
  quality: string;
}

export interface Latest {
  event_time: string | null;
  ingested_at: string | null;
  delay_seconds: number;
  operating_state: string;
  available: boolean;
  readings: Reading[];
}

export interface TurbineSummary {
  turbine_id: string;
  wind_farm_id: string;
  name: string;
  status: string;
  status_reason: string;
  rated_power_kw: number;
  latest: Latest | null;
}

export interface WindFarm {
  wind_farm_id: string;
  name: string;
  country: string;
  offshore: boolean;
  time_zone: string;
  location: { latitude: number; longitude: number };
  turbines: TurbineSummary[];
}

export interface Fleet {
  wind_farms: WindFarm[];
  telemetry_available: boolean;
}

export interface Component {
  component_id: string;
  parent_component_id: string;
  kind: string;
  name: string;
  manufacturer: string;
  model: string;
  serial_number: string;
  installed_on: string;
  status: string;
  status_reason: string;
}

export interface TurbineDetail {
  turbine_id: string;
  wind_farm_id: string;
  name: string;
  location: { latitude: number; longitude: number };
  model: {
    manufacturer: string;
    model: string;
    rated_power_kw: number;
    rotor_diameter_m: number;
    hub_height_m: number;
  };
  serial_number: string;
  commissioned_on: string;
  status: string;
  status_reason: string;
  status_changed_at: string | null;
  components: Component[];
  latest: Latest | null;
  telemetry_available: boolean;
}

export interface Point {
  time: string;
  value: number | null;
  quality: string;
}

export interface Series {
  metric: string;
  unit: string;
  points: Point[];
}

export interface TelemetryWindow {
  turbine_id: string;
  from_time: string;
  to_time: string;
  series: Series[];
}

/** Every call, bound to the signed-in user's token. */
export function fleetApi(token: string) {
  const turbine = (id: string) => `/fleet/turbines/${encodeURIComponent(id)}`;
  return {
    fleet: () => getJson<Fleet>("/fleet", token),
    turbine: (id: string) => getJson<TurbineDetail>(turbine(id), token),
    telemetry: (id: string, minutes: number, metrics: string[]) => {
      const query = new URLSearchParams({ minutes: String(minutes) });
      for (const metric of metrics) query.append("metric", metric);
      return getJson<TelemetryWindow>(`${turbine(id)}/telemetry?${query.toString()}`, token);
    },
  };
}

export type FleetApi = ReturnType<typeof fleetApi>;
