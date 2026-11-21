import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MaintenanceApi, WorkOrder, WorkOrderDetail } from "../api";
import { draftedBy } from "../api";
import { emptyPlan, fromLocalInput, fromPlan, toLocalInput, toPlan } from "../form";
import { NewDraft, WorkOrderList } from "./WorkOrderList";
import { WorkOrderPage } from "./WorkOrderPage";

const order: WorkOrder = {
  work_order_id: "wo_aaaaaaaaaaaaaaaaaaaa",
  version: 1,
  revision: 2,
  case_id: "case_bbbbbbbbbbbbbbbbbbbb",
  turbine_id: "dogger-east-t01",
  evidence_snapshot_id: "evs_c",
  plan: {
    title: "Oil cooler inspection",
    action: "inspection",
    summary: "Inspect the fan.",
    tasks: [{ description: "Inspect the fan", duration_hours: 3, crew_size: 2, requires_climb: true }],
    parts: [{ part_number: "GBX-FAN-CAP", quantity: 2 }],
    procedures: ["kn:GBX-OIL-01@3#12"],
    planned_start: "2026-12-01T08:00:00Z",
    planned_end: "2026-12-01T14:00:00Z",
    estimated_downtime_hours: 6,
    impact_result_id: "",
  },
  status: "draft",
  drafted_by: "service:fault-investigator",
  drafted_by_agent: "agent:maintenance-planner",
  created_at: "2026-11-20T09:00:00Z",
  updated_at: "2026-11-20T09:00:00Z",
  submitted_by: "",
  submitted_at: null,
  validation: {
    checked_at: "2026-11-20T09:00:00Z",
    passed: true,
    problems: [{ field: "parts[0]", code: "part_short", message: "GBX-FAN-CAP: 1 of 2 available", blocking: false }],
    turbine_status: "operating",
    policy_outcome: "escalate",
    policy_result_id: "pol_1",
    parts: [{ part_number: "GBX-FAN-CAP", quantity: 2, status: "short", available: 1 }],
  },
  stale_rule: "",
  stale_detail: "",
};

const detail: WorkOrderDetail = {
  earlier_versions: [],
  work_order: order,
  history: [
    {
      at: "2026-11-20T09:00:00Z",
      action: "drafted",
      actor_type: "agent",
      actor_id: "service:fault-investigator",
      agent: "agent:maintenance-planner",
      revision: 1,
      note: "",
    },
  ],
};

function fakeApi() {
  return {
    list: vi.fn(async () => ({ work_orders: [order], next_page_token: "" })),
    cases: vi.fn(async () => [
      { case_id: "case_bbbbbbbbbbbbbbbbbbbb", turbine_id: "dogger-east-t01", title: "Oil hot", status: "investigating", severity: "high" },
    ]),
    get: vi.fn(async () => detail),
    draft: vi.fn(async () => order),
    update: vi.fn(async () => ({ ...order, revision: 3 })),
    validate: vi.fn(async () => order),
    submit: vi.fn(async () => ({ ...order, status: "submitted" as const })),
    withdraw: vi.fn(async () => ({ ...order, status: "withdrawn" as const })),
    revise: vi.fn(async () => ({ ...order, version: 2 })),
  };
}

function show(element: React.ReactElement) {
  render(<MemoryRouter>{element}</MemoryRouter>);
}

afterEach(cleanup);

describe("the plan form", () => {
  it("turns what the planner typed into a plan, and back", () => {
    const form = { ...fromPlan(order.plan), parts: [...fromPlan(order.plan).parts, { part_number: " ", quantity: "1" }] };
    const plan = toPlan(form);
    expect(plan.parts).toEqual([{ part_number: "GBX-FAN-CAP", quantity: 2 }]);
    expect(plan.tasks[0]).toEqual({ description: "Inspect the fan", duration_hours: 3, crew_size: 2, requires_climb: true });
    expect(plan.planned_start).toBe("2026-12-01T08:00:00.000Z");
    expect(plan.procedures).toEqual(["kn:GBX-OIL-01@3#12"]);
  });

  it("keeps a window in the planner's own time, sent in UTC", () => {
    const local = toLocalInput("2026-12-01T08:00:00Z");
    expect(fromLocalInput(local)).toBe("2026-12-01T08:00:00.000Z");
    expect(fromLocalInput("")).toBeNull();
    expect(toPlan(emptyPlan()).planned_start).toBeNull();
  });
});

describe("the maintenance planner", () => {
  it("lists drafts first, marking an agent's draft", async () => {
    const api = fakeApi();
    show(<WorkOrderList api={api as unknown as MaintenanceApi} canDraft />);
    const row = await screen.findByTestId("work-order-wo_aaaaaaaaaaaaaaaaaaaa");
    expect(row.getAttribute("href")).toBe("/?work_order=wo_aaaaaaaaaaaaaaaaaaaa");
    expect(row.textContent).toContain("Agent's draft");
    expect(row.textContent).toContain("ready to submit");
    expect(api.list).toHaveBeenCalledWith(["draft"]);
    expect(screen.getByRole("link", { name: "New draft" })).toBeTruthy();
  });

  it("offers no new draft to someone who may only read", async () => {
    show(<WorkOrderList api={fakeApi() as unknown as MaintenanceApi} canDraft={false} />);
    await screen.findByTestId("work-order-wo_aaaaaaaaaaaaaaaaaaaa");
    expect(screen.queryByRole("link", { name: "New draft" })).toBeNull();
  });

  it("drafts for the chosen case", async () => {
    const api = fakeApi();
    show(<NewDraft api={api as unknown as MaintenanceApi} />);
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "Fan check" } });
    fireEvent.change(screen.getByLabelText("Task 1"), { target: { value: "Check the fan" } });
    fireEvent.change(screen.getByLabelText("Hours"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.draft).toHaveBeenCalled());
    const [, caseId, plan] = api.draft.mock.calls[0] as unknown as [string, string, { title: string; tasks: { duration_hours: number }[] }];
    expect(caseId).toBe("case_bbbbbbbbbbbbbbbbbbbb");
    expect(plan.title).toBe("Fan check");
    expect(plan.tasks[0]?.duration_hours).toBe(2);
  });

  it("shows an agent's draft with its parts, its problems, and who drafted it", async () => {
    const api = fakeApi();
    show(<WorkOrderPage api={api as unknown as MaintenanceApi} workOrderId={order.work_order_id} canDraft canSubmit />);
    expect(await screen.findByRole("note")).toBeTruthy();
    expect(screen.getByText(/the maintenance planner agent, for service:fault-investigator/)).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "Parts" })).getByText("Short: 1 in the store")).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "For the reviewer" })).getByText(/1 of 2 available/)).toBeTruthy();
    expect(within(screen.getByRole("list", { name: "History" })).getByText(/maintenance planner agent/)).toBeTruthy();
  });

  it("submits the revision it shows, and offers no submit without the permission", async () => {
    const api = fakeApi();
    show(<WorkOrderPage api={api as unknown as MaintenanceApi} workOrderId={order.work_order_id} canDraft canSubmit />);
    fireEvent.click(await screen.findByRole("button", { name: "Submit for a decision" }));
    await waitFor(() => expect(api.submit).toHaveBeenCalledWith(order.work_order_id, expect.any(String), 2));
    cleanup();
    show(<WorkOrderPage api={api as unknown as MaintenanceApi} workOrderId={order.work_order_id} canDraft canSubmit={false} />);
    await screen.findByRole("button", { name: "Edit plan" });
    expect(screen.queryByRole("button", { name: "Submit for a decision" })).toBeNull();
  });

  it("will not submit a draft that something blocks", async () => {
    const api = fakeApi();
    const blocked = {
      ...order,
      validation: { ...order.validation, passed: false, problems: [{ field: "procedures", code: "missing", message: "Cite one", blocking: true }] },
    };
    api.get.mockResolvedValue({ ...detail, work_order: blocked });
    show(<WorkOrderPage api={api as unknown as MaintenanceApi} workOrderId={order.work_order_id} canDraft canSubmit />);
    const submit = await screen.findByRole("button", { name: "Submit for a decision" });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    expect(within(screen.getByRole("list", { name: "Blocking problems" })).getByText("Cite one")).toBeTruthy();
  });

  it("edits a draft from the revision it shows", async () => {
    const api = fakeApi();
    show(<WorkOrderPage api={api as unknown as MaintenanceApi} workOrderId={order.work_order_id} canDraft canSubmit />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit plan" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.update).toHaveBeenCalled());
    const [id, , revision, plan] = api.update.mock.calls[0] as unknown as [string, string, number, { title: string }];
    expect([id, revision, plan.title]).toEqual([order.work_order_id, 2, "Renamed"]);
  });

  it("names a person who drafted it plainly", () => {
    expect(draftedBy({ ...order, drafted_by: "u-1", drafted_by_agent: "" })).toBe("u-1");
  });
});

describe("a stale work order", () => {
  it("shows the rule that fired, and is revised as the next version", async () => {
    const api = fakeApi();
    const stale = { ...order, status: "stale" as const, stale_rule: "inventory", stale_detail: "Part GBX-FAN-CAP is now short" };
    api.get.mockResolvedValue({ ...detail, work_order: stale, earlier_versions: [] });
    show(<WorkOrderPage api={api as unknown as MaintenanceApi} workOrderId={order.work_order_id} canDraft canSubmit />);
    const note = await screen.findByTestId("stale");
    expect(note.textContent).toContain("inventory");
    expect(note.textContent).toContain("cannot be approved");
    fireEvent.click(screen.getByRole("button", { name: "Revise as version 2" }));
    await waitFor(() => expect(api.revise).toHaveBeenCalledWith(order.work_order_id, expect.any(String), order.revision));
  });
});
