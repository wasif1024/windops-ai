// The investigation screens' calls to Web BFF (M11.1). The Investigation Service decides who may
// read a case, review its findings (FR-INV-006) or answer its verification; these only ask.
import { getJson, sendJson } from "../../api/client";

export type CaseStatus =
  | "open"
  | "investigating"
  | "pending_review"
  | "approved"
  | "verifying"
  | "verified"
  | "closed"
  | "cancelled";

export interface Investigation {
  case_id: string;
  turbine_id: string;
  title: string;
  status: CaseStatus;
  severity: string;
  /** "waiting", "running", "completed", "ended", "not_started", or "" with none. */
  investigation_status: string;
  run_id: string;
  created_at: string | null;
  updated_at: string | null;
  events: string;
}

export interface CaseEvent {
  at: string | null;
  action: string;
  actor_type: string;
  actor_id: string;
  note: string;
}

export interface Detection {
  detection_id: string;
  rule_id: string;
  rule_version: string;
  severity: string;
  attached_at: string | null;
}

export interface MetricComparison {
  metric: string;
  unit: string;
  before_mean: number | null;
  after_mean: number | null;
  before_maximum: number | null;
  after_maximum: number | null;
  before_readings: number;
  after_readings: number;
}

export interface RuleResult {
  rule_id: string;
  version: number;
  severity: string;
  summary: string;
  counts: boolean;
}

export interface Verification {
  work_order_id: string;
  proposal_version: number;
  completed_by: string;
  completed_at: string | null;
  status: "waiting" | "reported" | "not_started";
  outcome: "" | "resolved" | "not_resolved" | "inconclusive";
  window_start: string | null;
  window_end: string | null;
  after_samples: number;
  metrics: MetricComparison[];
  rules: RuleResult[];
  note: string;
  reported_at: string | null;
  review: "" | "closed" | "reopened";
  reviewed_by: string;
  reviewed_at: string | null;
  review_reason: string;
}

export interface CaseDetail {
  investigation: Investigation;
  origin: "detection" | "user";
  created_by: string;
  version: number;
  evidence_snapshot_id: string;
  evidence_version: number;
  evidence_status: "pending" | "ready";
  stale_rule: string;
  stale_detail: string;
  detections: Detection[];
  history: CaseEvent[];
  verifications: Verification[];
}

export type ReviewAction = "accept" | "reject" | "challenge" | "annotate";

export interface FindingReview {
  review_id: string;
  run_id: string;
  finding: string;
  action: ReviewAction;
  note: string;
  reviewed_by: string;
  reviewed_at: string | null;
}

/** The investigation's report (SRS 5.7): only what the screens show is typed. */
export interface ReportBody {
  observed_facts?: { statement: string; citations?: string[] }[];
  hypotheses?: { statement: string; support?: string[]; against?: string[]; confidence?: number }[];
  conflicting_evidence?: { description: string; citations?: string[] }[];
  data_gaps?: string[];
  risk_flags?: string[];
  recommended_actions?: { action?: string; summary?: string; downtime_hours?: number | null }[];
  confidence?: number;
  explanation?: string;
  escalation_required?: boolean;
}

export interface Report {
  run_id: string;
  report: ReportBody | null;
  reviews: FindingReview[];
}

export interface EvidenceItem {
  /** "asset", "telemetry", "alarm" or "fault-detection". */
  source_service: string;
  /** "turbine", "telemetry_snapshot", "alarm", "detection" or "fault_rule_version". */
  record_type: string;
  record_id: string;
  version: string;
  window_start: string | null;
  window_end: string | null;
  retrieved_at: string | null;
  /** For telemetry: each quality label's count, its gaps, rejections and samples. */
  data_quality: { quality?: Record<string, number>; gaps?: number; rejected?: number; samples?: number };
  content: Record<string, unknown>;
  content_hash: string;
}

export interface EvidenceSnapshot {
  snapshot_id: string;
  case_id: string;
  version: number;
  turbine_id: string;
  content_hash: string;
  hash_format: string;
  created_at: string | null;
  created_by: string;
  window_start: string | null;
  window_end: string | null;
  item_count: number;
  items: EvidenceItem[];
  verified: boolean;
}

export interface ProgressEvent {
  sequence: number;
  at: string | null;
  kind: string;
  run_id: string;
  position: number;
  total_steps: number;
  step: string;
  status: string;
  note: string;
}

export function investigationApi(token: string) {
  const one = (id: string) => `/investigations/${encodeURIComponent(id)}`;
  return {
    token,
    list: (every: boolean) => getJson<Investigation[]>(`/investigations${every ? "?all=true" : ""}`, token),
    detail: (id: string) => getJson<CaseDetail>(`${one(id)}/detail`, token),
    report: (id: string) => getJson<Report>(`${one(id)}/report`, token),
    snapshots: (id: string) => getJson<EvidenceSnapshot[]>(`${one(id)}/evidence`, token),
    snapshot: (snapshotId: string) => getJson<EvidenceSnapshot>(`/evidence/${encodeURIComponent(snapshotId)}`, token),
    freeze: (id: string, key: string) => sendJson<EvidenceSnapshot>("POST", `${one(id)}/evidence`, token, key),
    start: (key: string, turbineId: string, title: string, severity: string) =>
      sendJson<Investigation>("POST", "/investigations", token, key, { turbine_id: turbineId, title, severity }),
    review: (id: string, key: string, finding: string, action: ReviewAction, note: string) =>
      sendJson<FindingReview>("POST", `${one(id)}/findings`, token, key, { finding, action, note }),
    answer: (id: string, key: string, decision: "close" | "reopen", reason: string, version: number) =>
      sendJson<CaseDetail>("POST", `${one(id)}/verification`, token, key, {
        decision,
        reason,
        expected_version: version,
      }),
  };
}

export type InvestigationApi = ReturnType<typeof investigationApi>;

export const CASE_STYLE: Record<CaseStatus, string> = {
  open: "bg-sky-50 text-sky-800 ring-1 ring-sky-200",
  investigating: "bg-indigo-50 text-indigo-800 ring-1 ring-indigo-200",
  pending_review: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  approved: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  verifying: "bg-teal-50 text-teal-800 ring-1 ring-teal-200",
  verified: "bg-emerald-50 text-emerald-900 ring-1 ring-emerald-300",
  closed: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
  cancelled: "bg-slate-100 text-slate-500 ring-1 ring-slate-200",
};

export const SEVERITY_STYLE: Record<string, string> = {
  critical: "bg-red-100 text-red-900 ring-1 ring-red-300",
  high: "bg-orange-50 text-orange-900 ring-1 ring-orange-200",
  medium: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  low: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
};

export const REVIEW_STYLE: Record<ReviewAction, string> = {
  accept: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  reject: "bg-red-50 text-red-800 ring-1 ring-red-200",
  challenge: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  annotate: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
};

/** The reviews of one finding, oldest first, and the latest one's action. */
export function reviewsOf(reviews: FindingReview[], finding: string): { all: FindingReview[]; latest: FindingReview | undefined } {
  const all = reviews.filter((review) => review.finding === finding);
  return { all, latest: all.at(-1) };
}

/** Every reference the report cites, with the findings that cite it (FR-INV-004). */
export function citationsOf(report: ReportBody | null): Map<string, string[]> {
  const cited = new Map<string, string[]>();
  const add = (reference: string, finding: string) => cited.set(reference, [...(cited.get(reference) ?? []), finding]);
  if (!report) return cited;
  (report.observed_facts ?? []).forEach((fact, i) => fact.citations?.forEach((r) => add(r, `observed_facts[${i}]`)));
  (report.hypotheses ?? []).forEach((h, i) => {
    h.support?.forEach((r) => add(r, `hypotheses[${i}]`));
    h.against?.forEach((r) => add(r, `hypotheses[${i}] (against)`));
  });
  (report.conflicting_evidence ?? []).forEach((c, i) => c.citations?.forEach((r) => add(r, `conflicting_evidence[${i}]`)));
  for (const reference of (report as { evidence_references?: string[] }).evidence_references ?? []) {
    if (!cited.has(reference)) cited.set(reference, []);
  }
  return cited;
}

/** Where a reference the snapshot does not hold comes from, by its form. */
export function sourceOf(reference: string): string {
  if (reference.startsWith("kn:")) return "Knowledge library";
  if (/^https?:\/\//.test(reference)) return "External source";
  if (reference.startsWith("pol_")) return "Policy result";
  if (reference.startsWith("imp_")) return "Impact estimate";
  if (reference.startsWith("fct_")) return "Weather forecast";
  if (reference.startsWith("evs_")) return "Evidence snapshot";
  if (reference.startsWith("case_")) return "Case";
  return "Other record";
}

/** Findings whose latest review is a challenge or a rejection: what an approver must know. */
export function openConcerns(reviews: FindingReview[]): FindingReview[] {
  const latest = new Map<string, FindingReview>();
  for (const review of reviews) if (review.action !== "annotate") latest.set(review.finding, review);
  return [...latest.values()].filter((review) => review.action === "challenge" || review.action === "reject");
}
