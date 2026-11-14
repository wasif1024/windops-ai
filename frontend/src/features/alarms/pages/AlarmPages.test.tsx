import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Alarm, AlarmDetail, AlarmsApi } from "../api";
import { AlarmConsole } from "./AlarmConsole";
import { AlarmPage } from "./AlarmPage";

const alarm: Alarm = {
  alarm_id: "alm_1",
  turbine_id: "dogger-east-t01",
  code: "GBX_OIL_TEMP_HIGH",
  tag: "gearbox_oil_temp_c",
  description: "Gearbox oil temperature high",
  severity: "high",
  severity_source: "catalog:alarm-catalog-1",
  state: "active",
  source: "windops-simulator",
  raised_at: new Date(Date.now() - 4 * 60_000).toISOString(),
  received_at: null,
  acknowledged_at: null,
  acknowledged_by: "",
  cleared_at: null,
  shelved_until: null,
  shelved_by: "",
  shelve_reason: "",
  repeat_count: 2,
  version: 1,
};

const detail: AlarmDetail = {
  alarm,
  transitions: [{ at: alarm.raised_at, from_state: "", to_state: "active", actor_type: "device", actor_id: "dev_1", note: "" }],
};

function fakeApi() {
  return {
    list: vi.fn(async () => ({ alarms: [alarm], next_page_token: "" })),
    get: vi.fn(async () => detail),
    acknowledge: vi.fn(async () => ({ ...alarm, state: "acknowledged" as const })),
    shelve: vi.fn(async () => ({ ...alarm, state: "shelved" as const })),
    unshelve: vi.fn(async () => alarm),
  };
}

function show(element: React.ReactElement) {
  render(<MemoryRouter>{element}</MemoryRouter>);
}

afterEach(cleanup);

describe("the alarm console", () => {
  it("lists standing alarms with their severity, and links to each one", async () => {
    const api = fakeApi();
    show(<AlarmConsole api={api as unknown as AlarmsApi} />);
    const row = await screen.findByTestId("alarm-alm_1");
    expect(row.getAttribute("href")).toBe("/?alarm=alm_1");
    expect(row.textContent).toContain("High");
    expect(row.textContent).toContain("repeated 2×");
    expect(row.textContent).toContain("4 min ago");
    expect(api.list).toHaveBeenCalledWith(["active", "acknowledged", "shelved"]);
  });

  it("asks only for the chosen state", async () => {
    const api = fakeApi();
    api.list.mockResolvedValue({ alarms: [], next_page_token: "" });
    show(<AlarmConsole api={api as unknown as AlarmsApi} />);
    fireEvent.click(screen.getByRole("button", { name: "Cleared" }));
    expect(await screen.findByTestId("no-alarms")).toBeTruthy();
    expect(api.list).toHaveBeenLastCalledWith(["cleared"]);
    expect(screen.getByRole("button", { name: "Cleared" }).getAttribute("aria-pressed")).toBe("true");
  });
});

describe("one alarm", () => {
  it("shows where its severity came from and its history, and acknowledges it with a comment", async () => {
    const api = fakeApi();
    show(<AlarmPage api={api as unknown as AlarmsApi} alarmId="alm_1" canAct />);
    expect(await screen.findByRole("heading", { name: "Gearbox oil temperature high" })).toBeTruthy();
    expect(screen.getByText(/From the alarm catalogue, alarm-catalog-1/)).toBeTruthy();
    expect(screen.getByRole("list", { name: "History" }).textContent).toContain("Raised as active");
    fireEvent.change(screen.getByLabelText("Comment, if any"), { target: { value: " Crew informed " } });
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(api.acknowledge).toHaveBeenCalled());
    const [id, key, comment] = api.acknowledge.mock.calls[0] as unknown as [string, string, string];
    expect([id, comment]).toEqual(["alm_1", "Crew informed"]);
    expect(key).toBeTruthy();
  });

  it("needs a reason to shelve", async () => {
    show(<AlarmPage api={fakeApi() as unknown as AlarmsApi} alarmId="alm_1" canAct />);
    const shelve = await screen.findByRole("button", { name: "Shelve" });
    expect((shelve as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Reason to shelve"), { target: { value: "Sensor work" } });
    expect((shelve as HTMLButtonElement).disabled).toBe(false);
  });

  it("offers nothing to do to someone who may only read alarms", async () => {
    show(<AlarmPage api={fakeApi() as unknown as AlarmsApi} alarmId="alm_1" canAct={false} />);
    expect(await screen.findByText(/let you read alarms, not acknowledge/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
  });
});
