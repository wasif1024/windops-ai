import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Fleet, FleetApi } from "../api";
import { FleetPage } from "./FleetPage";

const fleet: Fleet = {
  telemetry_available: true,
  wind_farms: [
    {
      wind_farm_id: "dogger-east",
      name: "Dogger East",
      country: "GB",
      offshore: true,
      time_zone: "Europe/London",
      location: { latitude: 54.8, longitude: 1.9 },
      turbines: [
        {
          turbine_id: "dogger-east-t01",
          wind_farm_id: "dogger-east",
          name: "T01",
          status: "operating",
          status_reason: "",
          rated_power_kw: 8000,
          latest: {
            event_time: new Date().toISOString(),
            ingested_at: null,
            delay_seconds: 1,
            operating_state: "producing",
            available: true,
            readings: [
              { metric: "power", value: 6000, unit: "kW", quality: "good" },
              { metric: "wind_speed", value: 11.2, unit: "m/s", quality: "good" },
            ],
          },
        },
        {
          turbine_id: "dogger-east-t02",
          wind_farm_id: "dogger-east",
          name: "T02",
          status: "maintenance",
          status_reason: "Gearbox inspection",
          rated_power_kw: 8000,
          latest: null,
        },
      ],
    },
  ],
};

function show(answer: Fleet) {
  const api = { fleet: vi.fn(async () => answer) } as unknown as FleetApi;
  render(
    <MemoryRouter>
      <FleetPage api={api} />
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("the fleet overview", () => {
  it("shows each farm and turbine with its status, output and freshness", async () => {
    show(fleet);
    expect(await screen.findByRole("heading", { name: "Dogger East" })).toBeTruthy();
    expect(screen.getByText(/1 of 2 operating/)).toBeTruthy();
    const live = screen.getByTestId("turbine-dogger-east-t01");
    expect(live.getAttribute("href")).toBe("/?turbine=dogger-east-t01");
    expect(live.textContent).toContain("75%"); // 6,000 of a rated 8,000 kW
    expect(live.querySelector("[data-freshness]")?.getAttribute("data-freshness")).toBe("live");
    const idle = screen.getByTestId("turbine-dogger-east-t02");
    expect(idle.textContent).toContain("Maintenance");
    expect(idle.querySelector("[data-freshness]")?.getAttribute("data-freshness")).toBe("none");
  });

  it("says so when telemetry cannot be read, and still lists the turbines", async () => {
    show({ ...fleet, telemetry_available: false });
    expect(await screen.findByRole("status")).toBeTruthy();
    expect(screen.getByTestId("turbine-dogger-east-t01")).toBeTruthy();
  });
});
