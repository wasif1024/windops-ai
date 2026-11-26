// The audit explorer's calls to Web BFF (M11.6). Reading the trail and the deliveries needs
// audit.read, in the person's own tenant; nothing here can change a record.
import { getJson } from "../../api/client";

export interface AuditRecord {
  sequence: number;
  event_id: string;
  occurred_at: string | null;
  recorded_at: string | null;
  producer: string;
  action: string;
  actor_type: string;
  actor_id: string;
  on_behalf_of: string;
  resource_type: string;
  resource_id: string;
  outcome: string;
  correlation_id: string;
  previous_hash: string;
  record_hash: string;
}

export interface AuditPage {
  records: AuditRecord[];
  next_page_token: string;
}

export interface ChainCheck {
  valid: boolean;
  records_checked: number;
  first_invalid_sequence: number;
}

export interface DeliveryRecord {
  notification_id: string;
  kind: string;
  user_id: string;
  case_id: string;
  channel: string;
  status: string;
  provider: string;
  attempts: number;
  updated_at: string | null;
}

/** The filters the explorer offers, as they sit in the page's address. */
export const FILTERS = [
  "action_prefix",
  "actor_id",
  "resource_type",
  "resource_id",
  "outcome",
  "correlation_id",
  "producer",
  "from_time",
  "to_time",
] as const;
export type Filter = (typeof FILTERS)[number];
export type Filters = Partial<Record<Filter, string>>;

/** What a filter may hold, as Web BFF accepts it, so a typo is caught before it is sent. */
export const TEXT = /^[A-Za-z0-9_.:@#/-]{1,128}$/;

export function filtersFrom(params: URLSearchParams): Filters {
  const found: Filters = {};
  for (const name of FILTERS) {
    const value = params.get(name)?.trim();
    if (value) found[name] = value;
  }
  return found;
}

/** Whether every filter is one Web BFF accepts. */
export function validFilters(filters: Filters): boolean {
  return Object.entries(filters).every(([name, value]) => {
    if (name === "from_time" || name === "to_time") return !Number.isNaN(Date.parse(value));
    if (name === "outcome") return ["succeeded", "denied", "failed"].includes(value);
    return TEXT.test(value);
  });
}

function query(filters: Filters, extra: Record<string, string>): string {
  const params = new URLSearchParams(extra);
  for (const [name, value] of Object.entries(filters)) {
    // A time from the browser's datetime-local input is local time; the service wants UTC.
    params.set(name, name.endsWith("_time") ? new Date(value).toISOString() : value);
  }
  return params.toString();
}

export function auditApi(token: string) {
  return {
    records: (filters: Filters, newestFirst: boolean, pageToken = "") =>
      getJson<AuditPage>(
        `/audit/records?${query(filters, { newest_first: String(newestFirst), page_size: "50", ...(pageToken ? { page_token: pageToken } : {}) })}`,
        token,
      ),
    verify: () => getJson<ChainCheck>("/audit/verify", token),
    deliveries: (caseId: string, status: string) =>
      getJson<DeliveryRecord[]>(
        `/audit/deliveries?${new URLSearchParams({ page_size: "100", ...(caseId ? { case_id: caseId } : {}), ...(status ? { status } : {}) }).toString()}`,
        token,
      ),
  };
}

export type AuditApi = ReturnType<typeof auditApi>;

/** A long hash shortened for a list: its start and end. */
export function short(hash: string): string {
  return hash.length > 16 ? `${hash.slice(0, 8)}…${hash.slice(-6)}` : hash;
}
