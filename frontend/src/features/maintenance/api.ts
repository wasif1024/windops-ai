// The maintenance planner's calls to Web BFF (M9.4). The Maintenance Service validates every plan,
// decides who may draft, and lets only a person submit (FR-MNT-002, FR-MNT-003).
import { getJson, sendJson } from "../../api/client";

export type Action = "inspection" | "repair" | "replacement" | "adjustment" | "monitoring";
export type WorkOrderStatus = "draft" | "submitted" | "withdrawn";
export type PartStatus = "available" | "short" | "incompatible" | "retired" | "unknown";

export const ACTIONS: Action[] = ["inspection", "repair", "replacement", "adjustment", "monitoring"];

export interface Task {
  description: string;
  duration_hours: number;
  crew_size: number;
  requires_climb: boolean;
}

export interface PartLine {
  part_number: string;
  quantity: number;
}

export interface Plan {
  title: string;
  action: Action;
  summary: string;
  tasks: Task[];
  parts: PartLine[];
  procedures: string[];
  planned_start: string | null;
  planned_end: string | null;
  estimated_downtime_hours: number;
  impact_result_id: string;
}

export interface Problem {
  field: string;
  code: string;
  message: string;
  blocking: boolean;
}

export interface PartAvailability {
  part_number: string;
  quantity: number;
  status: PartStatus;
  available: number;
}

export interface Validation {
  checked_at: string | null;
  passed: boolean;
  problems: Problem[];
  turbine_status: string;
  /** "pass", "escalate" or "block"; "" if the Policy Service could not be asked. */
  policy_outcome: string;
  policy_result_id: string;
  parts: PartAvailability[];
}

export interface WorkOrder {
  work_order_id: string;
  version: number;
  revision: number;
  case_id: string;
  turbine_id: string;
  evidence_snapshot_id: string;
  plan: Plan;
  status: WorkOrderStatus;
  drafted_by: string;
  /** "agent:maintenance-planner" when an agent drafted it. */
  drafted_by_agent: string;
  created_at: string | null;
  updated_at: string | null;
  submitted_by: string;
  submitted_at: string | null;
  validation: Validation;
}

export interface WorkOrderEvent {
  at: string | null;
  action: string;
  actor_type: string;
  actor_id: string;
  agent: string;
  revision: number;
  note: string;
}

export interface WorkOrderDetail {
  work_order: WorkOrder;
  history: WorkOrderEvent[];
}

export interface ActiveCase {
  case_id: string;
  turbine_id: string;
  title: string;
  status: string;
  severity: string;
}

/** A plan as the Web BFF takes it: times as ISO 8601 with their zone. */
export type PlanIn = Omit<Plan, "planned_start" | "planned_end"> & {
  planned_start: string | null;
  planned_end: string | null;
};

export function maintenanceApi(token: string) {
  const one = (id: string) => `/work-orders/${encodeURIComponent(id)}`;
  return {
    list: (statuses: WorkOrderStatus[]) => {
      const query = new URLSearchParams({ page_size: "100" });
      for (const status of statuses) query.append("status", status);
      return getJson<{ work_orders: WorkOrder[]; next_page_token: string }>(`/work-orders?${query.toString()}`, token);
    },
    cases: () => getJson<ActiveCase[]>("/work-orders/cases", token),
    get: (id: string) => getJson<WorkOrderDetail>(one(id), token),
    draft: (key: string, caseId: string, plan: PlanIn) =>
      sendJson<WorkOrder>("POST", "/work-orders", token, key, { case_id: caseId, plan }),
    update: (id: string, key: string, revision: number, plan: PlanIn) =>
      sendJson<WorkOrder>("PUT", `${one(id)}/plan`, token, key, { expected_revision: revision, plan }),
    validate: (id: string, key: string) => sendJson<WorkOrder>("POST", `${one(id)}/validate`, token, key),
    submit: (id: string, key: string, revision: number) =>
      sendJson<WorkOrder>("POST", `${one(id)}/submit`, token, key, { expected_revision: revision }),
    withdraw: (id: string, key: string, reason: string) =>
      sendJson<WorkOrder>("POST", `${one(id)}/withdraw`, token, key, { reason }),
  };
}

export type MaintenanceApi = ReturnType<typeof maintenanceApi>;

export const STATUS_STYLE: Record<WorkOrderStatus, string> = {
  draft: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  submitted: "bg-sky-50 text-sky-800 ring-1 ring-sky-200",
  withdrawn: "bg-slate-100 text-slate-600 ring-1 ring-slate-200",
};

export const PART_STYLE: Record<PartStatus, string> = {
  available: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  short: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  incompatible: "bg-red-50 text-red-800 ring-1 ring-red-200",
  retired: "bg-red-50 text-red-800 ring-1 ring-red-200",
  unknown: "bg-red-50 text-red-800 ring-1 ring-red-200",
};

export const POLICY_STYLE: Record<string, string> = {
  pass: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  escalate: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  block: "bg-red-50 text-red-800 ring-1 ring-red-200",
};

/** Who drafted it, in words: a person, or the agent and the person it acted for. */
export function draftedBy(order: WorkOrder): string {
  if (!order.drafted_by_agent) return order.drafted_by;
  const agent = order.drafted_by_agent.replace(/^agent:/, "").replaceAll("-", " ");
  return `the ${agent} agent, for ${order.drafted_by}`;
}
