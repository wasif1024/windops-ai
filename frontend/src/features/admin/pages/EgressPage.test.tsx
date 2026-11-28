import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminApi, EgressPath } from "../api";
import { EgressPage } from "./EgressPage";

const forecasts: EgressPath = {
  path: "forecasts",
  enabled: true,
  destination: "Forecast provider: a site's coordinates and a time window",
  when_off: "No new forecasts.",
  reason: "",
  updated_by: "",
  updated_at: null,
};
const off: EgressPath = {
  ...forecasts,
  enabled: false,
  reason: "Coordinates not yet approved",
  updated_by: "u-admin",
  updated_at: "2026-11-28T10:00:00Z",
};

function fakeApi(paths: EgressPath[]) {
  const setEgress = vi.fn(async () => off);
  const api = { egress: vi.fn(async () => ({ paths })), setEgress } as unknown as AdminApi;
  return { api, setEgress };
}

afterEach(cleanup);

describe("the data egress page", () => {
  it("shows each path, where it goes, and what stops while it is off", async () => {
    render(<EgressPage api={fakeApi([forecasts]).api} />);
    const item = await screen.findByTestId("egress-forecasts");
    expect(within(item).getByText("Weather forecasts")).toBeTruthy();
    expect(within(item).getByText("On")).toBeTruthy();
    expect(within(item).getByText(/While off: No new forecasts\./)).toBeTruthy();
  });

  it("turns a path off only with a reason, and shows who did and why", async () => {
    const { api, setEgress } = fakeApi([forecasts]);
    render(<EgressPage api={api} />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn off" }));
    const [confirm] = screen.getAllByRole("button", { name: "Turn off" });
    if (!confirm) throw new Error("No confirm button");
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Why turn Weather forecasts off/), {
      target: { value: "Coordinates not yet approved" },
    });
    fireEvent.click(confirm);
    await vi.waitFor(() => expect(setEgress).toHaveBeenCalled());
    expect(setEgress.mock.calls[0]).toEqual([expect.any(String), "forecasts", false, "Coordinates not yet approved"]);
  });

  it("says plainly what a path turned off has stopped", async () => {
    render(<EgressPage api={fakeApi([off]).api} />);
    const item = await screen.findByTestId("egress-forecasts");
    expect(within(item).getByText("Off")).toBeTruthy();
    expect(within(item).getByText(/Now: No new forecasts\./)).toBeTruthy();
    expect(within(item).getByText(/Coordinates not yet approved/)).toBeTruthy();
  });
});
