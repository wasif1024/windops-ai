import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../../api/client";
import type { MaintenanceApi, WorkOrderDetail } from "../../maintenance/api";
import type { Approval, ApprovalApi } from "../api";
import { ApprovalPage, ApprovalQueue } from "./ApprovalScreen";

const pending: Approval = {
  approval_id: "apr_aaaaaaaaaaaaaaaaaaaa",
  work_order_id: "wo_bbbbbbbbbbbbbbbbbbbb",
  proposal_version: 1,
  case_id: "case_cccccccccccccccccccc",
  turbine_id: "dogger-east-t01",
  evidence_snapshot_id: "evs_dddddddddddddddddddd",
  status: "pending",
  submitted_by: "u-planner",
  drafted_by: "service:fault-investigator",
  drafted_by_agent: "agent:maintenance-planner",
  submitted_at: "2026-11-21T09:00:00Z",
  requested_at: "2026-11-21T09:00:01Z",
  decision: null,
  superseded_reason: "",
  superseded_at: null,
  standing: "decidable",
  standing_reason: "",
};

const approved: Approval = {
  ...pending,
  status: "approved",
  decision: {
    outcome: "approved",
    decided_by: "u-manager",
    decided_at: "2026-11-21T10:00:00Z",
    reason: "Fits the findings",
    proposal_version: 1,
    evidence_snapshot_id: pending.evidence_snapshot_id,
    requesters: ["u-planner"],
    policy: {
      policy_result_id: "pol_1",
      policy_outcome: "escalate",
      validated_at: "2026-11-21T08:59:00Z",
      case_status: "pending_review",
      evidence_version: 1,
      work_order_revision: 4,
      approval_rules: "separation-of-duties-1",
      permissions_version: 2,
    },
  },
};

const detail = {
  work_order: {
    work_order_id: pending.work_order_id,
    version: 1,
    revision: 4,
    case_id: pending.case_id,
    turbine_id: pending.turbine_id,
    evidence_snapshot_id: pending.evidence_snapshot_id,
    plan: {
      title: "Oil cooler inspection",
      action: "inspection",
      summary: "",
      tasks: [],
      parts: [],
      procedures: [],
      planned_start: null,
      planned_end: null,
      estimated_downtime_hours: 0,
      impact_result_id: "",
    },
    status: "submitted",
    drafted_by: pending.drafted_by,
    drafted_by_agent: pending.drafted_by_agent,
    created_at: null,
    updated_at: null,
    submitted_by: "u-planner",
    submitted_at: null,
    validation: { checked_at: null, passed: true, problems: [], turbine_status: "operating", policy_outcome: "pass", policy_result_id: "pol_1", parts: [] },
  },
  history: [],
} as unknown as WorkOrderDetail;

function fakes(decide: () => Promise<Approval>) {
  const api = {
    list: vi.fn(async () => ({ approvals: [pending], next_page_token: "" })),
    get: vi.fn(async () => pending),
    decide: vi.fn(decide),
  };
  const maintenance = { get: vi.fn(async () => detail) };
  return { api, maintenance };
}

function show(element: React.ReactElement) {
  render(<MemoryRouter>{element}</MemoryRouter>);
}

afterEach(cleanup);

describe("the approval queue", () => {
  it("lists pending requests first, marking an agent's draft", async () => {
    const { api } = fakes(async () => approved);
    show(<ApprovalQueue api={api as unknown as ApprovalApi} />);
    const row = await screen.findByTestId("approval-apr_aaaaaaaaaaaaaaaaaaaa");
    expect(row.getAttribute("href")).toBe("/?approval=apr_aaaaaaaaaaaaaaaaaaaa");
    expect(row.textContent).toContain("Agent's draft");
    expect(api.list).toHaveBeenCalledWith(["pending"]);
  });

  it("approves exactly the version and snapshot shown, then shows the decision", async () => {
    const { api, maintenance } = fakes(async () => approved);
    api.get.mockResolvedValueOnce(pending).mockResolvedValue(approved);
    show(
      <ApprovalPage
        api={api as unknown as ApprovalApi}
        maintenance={maintenance as unknown as MaintenanceApi}
        approvalId={pending.approval_id}
        canDecide
      />,
    );
    expect(await screen.findByText("Oil cooler inspection")).toBeTruthy();
    expect(screen.getByRole("note").textContent).toContain("An agent drafted this");
    const reject = screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement;
    expect(reject.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(api.decide).toHaveBeenCalled());
    const [id, , sent, decision] = api.decide.mock.calls[0] as unknown as [string, string, Approval, string];
    expect([id, sent.proposal_version, sent.evidence_snapshot_id, decision]).toEqual([
      pending.approval_id,
      1,
      pending.evidence_snapshot_id,
      "approve",
    ]);
    const shown = await screen.findByTestId("decision");
    expect(shown.textContent).toContain("u-manager");
    expect(shown.textContent).toContain("separation-of-duties-1");
  });

  it("explains a refusal to someone who had a hand in it, in its own words", async () => {
    const { api, maintenance } = fakes(async () => {
      throw new ApiError({ title: "x", status: 403, code: "requester" });
    });
    show(
      <ApprovalPage
        api={api as unknown as ApprovalApi}
        maintenance={maintenance as unknown as MaintenanceApi}
        approvalId={pending.approval_id}
        canDecide
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect((await screen.findByRole("alert")).textContent).toContain("another manager must decide it");
  });

  it("offers no decision to someone who may only read", async () => {
    const { api, maintenance } = fakes(async () => approved);
    show(
      <ApprovalPage
        api={api as unknown as ApprovalApi}
        maintenance={maintenance as unknown as MaintenanceApi}
        approvalId={pending.approval_id}
        canDecide={false}
      />,
    );
    expect(await screen.findByText("Waiting for a Maintenance Manager to decide.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("keeps what you may not decide apart, with why: a stale one is never offered", async () => {
    const stale: Approval = {
      ...pending,
      approval_id: "apr_ssssssssssssssssssss",
      standing: "stale",
      standing_reason: "The work order is stale: inventory (Part P-1 is now short)",
    };
    const { api, maintenance } = fakes(async () => approved);
    api.list.mockResolvedValue({ approvals: [pending, stale], next_page_token: "" });
    show(<ApprovalQueue api={api as unknown as ApprovalApi} />);
    const others = await screen.findByRole("list", { name: "Not yours to decide now" });
    expect(others.textContent).toContain("Stale");
    expect(others.textContent).toContain("Part P-1 is now short");
    expect(screen.getByRole("list", { name: "Requests" }).textContent).toContain(pending.work_order_id);
    cleanup();

    api.get.mockResolvedValue(stale);
    show(
      <ApprovalPage
        api={api as unknown as ApprovalApi}
        maintenance={maintenance as unknown as MaintenanceApi}
        approvalId={stale.approval_id}
        canDecide
      />,
    );
    expect((await screen.findByTestId("standing")).textContent).toContain("inventory");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});
