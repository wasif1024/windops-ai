// The alarm console's calls to Web BFF (M5.6). The Alarm Service decides who sees and may act on
// each alarm, and never lets anyone change an alarm's severity (FR-ALM-004).
import { getJson, sendJson } from "../../api/client";

export type AlarmState = "active" | "acknowledged" | "shelved" | "cleared";
export type Severity = "low" | "medium" | "high" | "critical";

export interface Alarm {
  alarm_id: string;
  turbine_id: string;
  code: string;
  tag: string;
  description: string;
  severity: Severity;
  /** "device", or "catalog:<version>" when the device sent none. */
  severity_source: string;
  state: AlarmState;
  source: string;
  raised_at: string | null;
  received_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by: string;
  cleared_at: string | null;
  shelved_until: string | null;
  shelved_by: string;
  shelve_reason: string;
  repeat_count: number;
  version: number;
}

export interface Transition {
  at: string | null;
  /** "" for the raise, which has no state before it. */
  from_state: string;
  to_state: AlarmState;
  /** "device", "user" or "system". */
  actor_type: string;
  actor_id: string;
  note: string;
}

export interface AlarmList {
  alarms: Alarm[];
  next_page_token: string;
}

export interface AlarmDetail {
  alarm: Alarm;
  transitions: Transition[];
}

export function alarmsApi(token: string) {
  const one = (id: string) => `/alarms/${encodeURIComponent(id)}`;
  return {
    list: (states: AlarmState[], turbineId = "") => {
      const query = new URLSearchParams({ page_size: "500" });
      for (const state of states) query.append("state", state);
      if (turbineId) query.set("turbine_id", turbineId);
      return getJson<AlarmList>(`/alarms?${query.toString()}`, token);
    },
    get: (id: string) => getJson<AlarmDetail>(one(id), token),
    acknowledge: (id: string, key: string, comment: string) =>
      sendJson<Alarm>("POST", `${one(id)}/acknowledge`, token, key, { comment }),
    shelve: (id: string, key: string, reason: string, minutes: number) =>
      sendJson<Alarm>("POST", `${one(id)}/shelve`, token, key, { reason, minutes }),
    unshelve: (id: string, key: string) => sendJson<Alarm>("POST", `${one(id)}/unshelve`, token, key),
  };
}

export type AlarmsApi = ReturnType<typeof alarmsApi>;

export const STANDING: AlarmState[] = ["active", "acknowledged", "shelved"];

/** Most severe first, as the console and the Alarm Service order them. */
export const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low"];

export const SEVERITY_STYLE: Record<Severity, string> = {
  critical: "bg-red-700 text-white",
  high: "bg-orange-100 text-orange-900",
  medium: "bg-amber-100 text-amber-900",
  low: "bg-slate-100 text-slate-700",
};

export const STATE_STYLE: Record<AlarmState, string> = {
  active: "bg-red-50 text-red-800 ring-1 ring-red-200",
  acknowledged: "bg-sky-50 text-sky-800 ring-1 ring-sky-200",
  shelved: "bg-violet-50 text-violet-800 ring-1 ring-violet-200",
  cleared: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
};

/** How long ago, as an operator reads it: "just now", "4 min ago", "3 h ago", "2 d ago". */
export function ago(when: string | null, now: number): string {
  if (!when) return "—";
  const seconds = Math.max(0, Math.round((now - Date.parse(when)) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86_400)} d ago`;
}

/** Where its severity came from, in words. */
export function severitySource(source: string): string {
  if (source === "device") return "From the turbine's controller";
  return source.startsWith("catalog:") ? `From the alarm catalogue, ${source.slice(8)}` : source;
}
