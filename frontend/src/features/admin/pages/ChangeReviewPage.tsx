// Change review (SRS 9.4): what administrators changed in the tenant, by kind, newest first, from
// the tamper-evident audit trail. Each change names who made it and when; the audit explorer has
// every detail. MCP integrations and gateway policies change only by pull request and release, so
// their review is in their own tabs, against the release.
import { useCallback, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useLoad } from "../../../api/useLoad";
import { Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { auditApi } from "../../audit/api";

/** Each kind of change, by the audit actions that record it. */
export const KINDS = [
  { id: "users", title: "Users", prefix: "identity.user." },
  { id: "sessions", title: "Sessions", prefix: "identity.sessions." },
  { id: "roles", title: "Roles", prefix: "authorization.role." },
  { id: "permissions", title: "Role permissions", prefix: "authorization.role_permissions." },
  { id: "approval", title: "Approval policy", prefix: "authorization.approval_policy." },
  { id: "break-glass", title: "Break-glass", prefix: "authorization.break_glass." },
  { id: "tenant", title: "Branding and domain", prefix: "tenant." },
  { id: "notifications", title: "Notification rules", prefix: "notification.rules" },
  { id: "knowledge", title: "Knowledge settings", prefix: "knowledge_settings." },
] as const;

const OUTCOME: Record<string, string> = {
  succeeded: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  denied: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  failed: "bg-red-50 text-red-800 ring-1 ring-red-200",
};

export function ChangeReviewPage({ token, emails }: { token: string; emails: Map<string, string> | null }) {
  const api = useMemo(() => auditApi(token), [token]);
  const [params, setParams] = useSearchParams();
  const kind = KINDS.find((k) => k.id === params.get("kind")) ?? KINDS[0];
  const load = useCallback(() => api.records({ action_prefix: kind.prefix }, true), [api, kind.prefix]);
  const [state] = useLoad(load);
  const name = (id: string) => emails?.get(id) ?? id;
  return (
    <Panel title="Change review">
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-2" role="group" aria-label="Kind of change">
          {KINDS.map((k) => (
            <button
              key={k.id}
              type="button"
              aria-pressed={k.id === kind.id}
              onClick={() => {
                const next = new URLSearchParams(params);
                next.set("kind", k.id);
                setParams(next);
              }}
              className={`rounded-md px-3 py-1 ring-1 ${k.id === kind.id ? "bg-sky-50 ring-sky-300" : "ring-slate-200 hover:bg-slate-50"}`}
            >
              {k.title}
            </button>
          ))}
        </div>
        <Loading state={state}>
          {({ records }) =>
            records.length === 0 ? (
              <p className="text-slate-500">No {kind.title.toLowerCase()} changes are recorded.</p>
            ) : (
              <ul className="divide-y divide-slate-100" aria-label="Changes" data-testid="changes">
                {records.map((r) => (
                  <li key={r.sequence} className="py-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="break-all font-mono text-slate-900">{r.action}</span>
                      <StatusBadge status={r.outcome} className={OUTCOME[r.outcome] ?? ""} />
                    </div>
                    <p className="mt-0.5 break-words text-xs text-slate-500">
                      {when(r.occurred_at)} · by {r.actor_type === "user" ? name(r.actor_id) : `${r.actor_type} ${r.actor_id}`}
                      {r.resource_id && ` · ${r.resource_type} ${name(r.resource_id)}`}
                    </p>
                  </li>
                ))}
              </ul>
            )
          }
        </Loading>
        <Link
          to={`/screens/audit?action_prefix=${encodeURIComponent(kind.prefix)}`}
          className="inline-block text-brand-primary hover:underline"
        >
          See these in the audit explorer
        </Link>
      </div>
    </Panel>
  );
}
