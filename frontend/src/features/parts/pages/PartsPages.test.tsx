import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Part, PartsApi } from "../api";
import { fitsText } from "../api";
import { PartPage, PartsLookup } from "./PartsScreen";

const part: Part = {
  part_number: "GBX-FAN/CAP.25",
  name: "Cooler fan capacitor",
  description: "25 µF",
  component_kind: "gearbox",
  unit: "each",
  fits: [{ manufacturer: "WindOps Synthetic", model: "WS-8.0-167" }],
  fits_any_model: false,
  retired: false,
  version: 1,
};

function fakeApi() {
  return {
    search: vi.fn(async () => ({
      turbine_id: "dogger-east-t01",
      wind_farm_id: "dogger-east",
      model: { manufacturer: "WindOps Synthetic", model: "WS-8.0-167" },
      parts: [{ part, stock: { wind_farm_id: "dogger-east", on_hand: 4, reserved: 1, available: 3, updated_at: null } }],
      next_page_token: "",
    })),
    detail: vi.fn(async () => ({
      part,
      stock: [
        { wind_farm_id: "dogger-east", on_hand: 4, reserved: 1, available: 3, updated_at: null },
        { wind_farm_id: "hornsea-west", on_hand: 0, reserved: 0, available: 0, updated_at: null },
      ],
    })),
  };
}

afterEach(cleanup);

describe("parts lookup", () => {
  it("finds the parts that fit a turbine, with its store's stock", async () => {
    const api = fakeApi();
    render(
      <MemoryRouter>
        <PartsLookup api={api as unknown as PartsApi} />
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByLabelText("Fits turbine"), { target: { value: "dogger-east-t01" } });
    fireEvent.change(screen.getByLabelText("Words"), { target: { value: "fan" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(api.search).toHaveBeenLastCalledWith({ turbine_id: "dogger-east-t01", component_kind: "", query: "fan" }),
    );
    const row = await screen.findByTestId("part-GBX-FAN/CAP.25");
    expect(row.textContent).toContain("3 available");
    expect(row.getAttribute("href")).toContain("part=GBX-FAN%2FCAP.25");
    expect(screen.getByTestId("parts-context").textContent).toContain("WS-8.0-167");
  });

  it("shows a part's stock in each store, and what it fits", async () => {
    render(
      <MemoryRouter initialEntries={["/?part=GBX-FAN%2FCAP.25&turbine=dogger-east-t01"]}>
        <PartPage api={fakeApi() as unknown as PartsApi} partNumber="GBX-FAN/CAP.25" />
      </MemoryRouter>,
    );
    const stores = await screen.findByRole("list", { name: "Stores" });
    expect(within(stores).getByText("hornsea-west")).toBeTruthy();
    expect(within(stores).getByText("3 available")).toBeTruthy();
    expect(screen.getByText("WindOps Synthetic WS-8.0-167")).toBeTruthy();
    expect(screen.getByRole("link", { name: "← Parts lookup" }).getAttribute("href")).toBe("/?turbine=dogger-east-t01");
  });

  it("says when a consumable fits any model", () => {
    expect(fitsText({ ...part, fits: [], fits_any_model: true })).toBe("Any model");
  });
});
