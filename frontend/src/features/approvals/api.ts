// The approval queue's calls to Web BFF (M10.2). The Approval Service decides who may decide,
// rechecks every request against the current work order and evidence, and refuses any agent, or
// anyone who had a hand in the work order (FR-APR-001, RBAC-004 to RBAC-008).
import { getJson, sendJson } from "../../api/client";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "superseded";

export interface PolicyContext {
  policy_result_id: string;
  policy_outcome: string;
  validated_at: string | null;
  case_status: string;
  evidence_version: number;
  work_order_revision: number;
  approval_rules: string;
  permissions_version: number;
}

export interface Decision {
  outcome: "approved" | "rejected";
  decided_by: string;
  decided_at: string | null;
  reason: string;
  proposal_version: number;
  evidence_snapshot_id: string;
  policy: PolicyContext;
  requesters: string[];
}

export interface Approval {
  approval_id: string;
  work_order_id: string;
  proposal_version: number;
  case_id: string;
  turbine_id: string;
  evidence_snapshot_id: string;
  status: ApprovalStatus;
  submitted_by: string;
  drafted_by: string;
  drafted_by_agent: string;
  submitted_at: string | null;
  requested_at: string | null;
  decision: Decision | null;
  superseded_reason: string;
  superseded_at: string | null;
}

export function approvalApi(token: string) {
  const one = (id: string) => `/approvals/${encodeURIComponent(id)}`;
  return {
    list: (statuses: ApprovalStatus[]) => {
      const query = new URLSearchParams({ page_size: "100" });
      for (const status of statuses) query.append("status", status);
      return getJson<{ approvals: Approval[]; next_page_token: string }>(`/approvals?${query.toString()}`, token);
    },
    get: (id: string) => getJson<Approval>(one(id), token),
    decide: (id: string, key: string, approval: Approval, decision: "approve" | "reject", reason: string) =>
      sendJson<Approval>("POST", `${one(id)}/decision`, token, key, {
        decision,
        reason,
        expected_proposal_version: approval.proposal_version,
        expected_evidence_snapshot_id: approval.evidence_snapshot_id,
      }),
  };
}

export type ApprovalApi = ReturnType<typeof approvalApi>;

export const APPROVAL_STYLE: Record<ApprovalStatus, string> = {
  pending: "bg-sky-50 text-sky-800 ring-1 ring-sky-200",
  approved: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  rejected: "bg-red-50 text-red-800 ring-1 ring-red-200",
  superseded: "bg-slate-100 text-slate-600 ring-1 ring-slate-200",
};

export function draftedByOf(approval: Approval): string {
  if (!approval.drafted_by_agent) return approval.drafted_by;
  const agent = approval.drafted_by_agent.replace(/^agent:/, "").replaceAll("-", " ");
  return `the ${agent} agent, for ${approval.drafted_by}`;
}
