// Changes to who may approve maintenance, held until a second person confirms them (RBAC-007).
// The Authorization Service decides who may: never the proposer; another Tenant Administrator;
// or a Platform Administrator only where the tenant has no other.
import { useCallback, useState } from "react";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import type { PendingChange } from "../api";

export interface PolicyChangesApi {
  policyChanges: () => Promise<{ changes: PendingChange[] }>;
  confirmChange: (key: string, item: PendingChange) => Promise<PendingChange>;
  declineChange: (key: string, item: PendingChange, reason: string) => Promise<PendingChange>;
}

const STATUS_STYLE: Record<PendingChange["status"], string> = {
  pending: "bg-amber-50 text-amber-900 ring-amber-200",
  confirmed: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  declined: "bg-slate-100 text-slate-600 ring-slate-200",
  expired: "bg-slate-100 text-slate-600 ring-slate-200",
};

function Change({ api, item, onDone }: { api: PolicyChangesApi; item: PendingChange; onDone: () => void }) {
  const change = useChange();
  const [reason, setReason] = useState("");
  const busy = change.state.kind === "busy";
  const run = async (action: (key: string) => Promise<PendingChange>) => {
    if (await change.run(action)) onDone();
  };
  return (
    <li className="rounded-lg border border-slate-200 p-3" data-testid={`change-${item.change_id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium ring-1 ${STATUS_STYLE[item.status]}`}>
          {item.status.charAt(0).toUpperCase() + item.status.slice(1)}
        </span>
        {item.escalated && <span className="text-xs text-violet-700">Escalated: no second Tenant Administrator</span>}
      </div>
      <p className="mt-2 break-words font-medium text-slate-900">{item.summary}</p>
      <p className="break-words text-xs text-slate-500">
        {item.tenant_id} · proposed by {item.proposed_by} {when(item.proposed_at)}
        {item.status === "pending" ? ` · expires ${when(item.expires_at)}` : ""}
        {item.decided_by ? ` · ${item.status} by ${item.decided_by} ${when(item.decided_at)}` : ""}
      </p>
      {item.decline_reason && <p className="mt-1 break-words text-sm text-slate-700">“{item.decline_reason}”</p>}
      {item.status === "pending" && (
        <div className="mt-3 space-y-2">
          <Button disabled={busy} onClick={() => void run((key) => api.confirmChange(key, item))}>
            Confirm
          </Button>
          <label className={LABEL} htmlFor={`decline-${item.change_id}`}>
            Reason to decline or withdraw
          </label>
          <input
            id={`decline-${item.change_id}`}
            className={INPUT}
            maxLength={500}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <Button
            variant="outline"
            className="text-red-700"
            disabled={busy || !reason.trim()}
            onClick={() => void run((key) => api.declineChange(key, item, reason.trim()))}
          >
            Decline
          </Button>
        </div>
      )}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </li>
  );
}

/** The changes, pending first, each confirmed or declined by someone other than its proposer. */
export function PolicyChangesPage({ api, title, empty }: { api: PolicyChangesApi; title: string; empty: string }) {
  const [state, reload] = useLoad(useCallback(() => api.policyChanges(), [api]));
  return (
    <Panel title={title}>
      <p className="mb-3 text-sm text-slate-500">
        A change to who may approve maintenance waits for a second administrator, never the one who proposed it.
      </p>
      <Loading state={state}>
        {({ changes }) => {
          const ordered = [...changes].sort((a, b) => Number(b.status === "pending") - Number(a.status === "pending"));
          return ordered.length === 0 ? (
            <p className="text-slate-500" data-testid="no-policy-changes">
              {empty}
            </p>
          ) : (
            <ul className="space-y-2" aria-label="Changes">
              {ordered.map((item) => (
                <Change key={item.change_id} api={api} item={item} onDone={reload} />
              ))}
            </ul>
          );
        }}
      </Loading>
    </Panel>
  );
}
