import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { duration, type Forecast } from "../weather/api";
import { WeatherScreen } from "../weather/pages/WeatherScreen";
import { amount, figureName, placeIn } from "./api";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("impact figures", () => {
  it("names a figure in words and shows it in its unit", () => {
    expect(figureName("projected_lost_energy_mwh")).toBe("Projected lost energy");
    expect(figureName("total_cost_eur")).toBe("Total cost");
    expect(amount(9.14, "MWh")).toBe("9.1 MWh");
    expect(amount(12500, "EUR")).toBe(`€${(12500).toLocaleString()}`);
  });

  it("places the value inside its range for the bar", () => {
    expect(placeIn({ name: "x", value: 9, low: 6, high: 12, unit: "MWh" })).toEqual({ low: 50, value: 75 });
    expect(placeIn({ name: "x", value: 0, low: 0, high: 0, unit: "MWh" })).toEqual({ low: 0, value: 100 });
  });
});

const FORECAST: Forecast = {
  turbine_id: "dogger-east-t01",
  wind_farm_id: "dogger-east",
  state: "stale",
  note: "The provider cannot be reached",
  age_seconds: 4 * 3600,
  provider: "open-meteo",
  model: "best_match",
  revision: "",
  forecast_id: "fct_1",
  retrieved_at: "2026-11-26T06:00:00Z",
  licence: "CC BY 4.0",
  from_time: null,
  to_time: null,
  summary: {
    max_wind_speed_hub_mps: 11.5,
    max_wind_gusts_10m_mps: 16.2,
    total_precipitation_mm: 0.4,
    min_visibility_m: 8000,
    max_cape_j_per_kg: null,
    hours_covered: 24,
    hours_wanted: 24,
  },
  points: [
    {
      valid_at: "2026-11-26T10:00:00Z",
      wind_speed_hub_mps: 9.4,
      wind_gusts_10m_mps: 12,
      wind_speed_10m_mps: 7,
      wind_direction_10m_deg: 240,
      temperature_2m_c: 8,
      precipitation_mm: 0,
      visibility_m: 10000,
      cape_j_per_kg: null,
    },
  ],
};

const FLEET = {
  telemetry_available: true,
  wind_farms: [
    {
      wind_farm_id: "dogger-east",
      name: "Dogger East",
      country: "GB",
      offshore: true,
      time_zone: "Europe/London",
      location: { latitude: 54, longitude: 1 },
      turbines: [
        {
          turbine_id: "dogger-east-t01",
          wind_farm_id: "dogger-east",
          name: "DE T01",
          status: "operating",
          status_reason: "",
          rated_power_kw: 3000,
          latest: { event_time: new Date().toISOString(), ingested_at: null, delay_seconds: 1, operating_state: "producing", available: true, readings: [] },
        },
      ],
    },
  ],
};

describe("the weather and source freshness panel", () => {
  it("says a forecast is too old for the work-window rules, and the telemetry is live", async () => {
    expect(duration(4 * 3600)).toBe("4 h");
    expect(duration(3 * 3600 + 300)).toBe("3 h 5 min");
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(new Response(JSON.stringify(url.includes("/weather/") ? FORECAST : FLEET), { status: 200 })),
      ),
    );
    render(
      <MemoryRouter initialEntries={["/?turbine=dogger-east-t01"]}>
        <WeatherScreen token="token" />
      </MemoryRouter>,
    );
    const freshness = await screen.findByTestId("freshness");
    expect(freshness.textContent).toContain("Stale");
    expect(freshness.textContent).toContain("4 h old");
    expect(freshness.textContent).toContain("Too old: work at height is blocked until it is fresh");
    expect(freshness.textContent).toContain("Live");
    expect(screen.getByTestId("forecast-summary").textContent).toContain("11.5 m/s");
    expect(screen.getByText("CC BY 4.0")).toBeTruthy();
  });
});
