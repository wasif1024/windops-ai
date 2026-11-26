// The audit explorer (SRS 4.11, M11.6): the tenant's tamper-evident audit trail, newest first,
// narrowed by who acted, on what, with what outcome, in one request and when; each record's hash
// chain checked again on demand; and what notifications reached whom. Read-only: nothing here can
// change a record.
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import {
  type AuditApi,
  auditApi,
  type AuditRecord,
  type ChainCheck,
  type Filter,
  FILTERS,
  type Filters,
  filtersFrom,
  short,
  validFilters,
} from "../api";

const OUTCOME_STYLE: Record<string, string> = {
  succeeded: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  denied: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  failed: "bg-red-50 text-red-800 ring-1 ring-red-200",
};

const LABELS: Record<Filter, string> = {
  action_prefix: "Action starts with",
  actor_id: "Actor",
  resource_type: "Resource type",
  resource_id: "Resource",
  outcome: "Outcome",
  correlation_id: "Request",
  producer: "Service",
  from_time: "From",
  to_time: "Until",
};

function FilterForm({ applied, onApply }: { applied: Filters; onApply: (f: Filters) => void }) {
  const [draft, setDraft] = useState<Filters>(applied);
  const fine = validFilters(Object.fromEntries(Object.entries(draft).filter(([, v]) => v)));
  return (
    <form
      aria-label="Filter the trail"
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        onApply(Object.fromEntries(Object.entries(draft).filter(([, v]) => v?.trim())) as Filters);
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {FILTERS.map((name) => (
          <div key={name}>
            <label className={LABEL} htmlFor={`audit-${name}`}>
              {LABELS[name]}
            </label>
            {name === "outcome" ? (
              <select
                id={`audit-${name}`}
                className={INPUT}
                value={draft.outcome ?? ""}
                onChange={(e) => setDraft({ ...draft, outcome: e.target.value })}
              >
                <option value="">Any</option>
                <option value="succeeded">Succeeded</option>
                <option value="denied">Denied</option>
                <option value="failed">Failed</option>
              </select>
            ) : (
              <input
                id={`audit-${name}`}
                className={INPUT}
                type={name.endsWith("_time") ? "datetime-local" : "text"}
                maxLength={128}
                placeholder={name === "action_prefix" ? "Such as knowledge_document." : undefined}
                value={draft[name] ?? ""}
                onChange={(e) => setDraft({ ...draft, [name]: e.target.value })}
              />
            )}
          </div>
        ))}
      </div>
      {!fine && <p className="text-sm text-red-800">Filters hold letters, digits and . _ : @ # / - only.</p>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={!fine}>
          Apply
        </Button>
        <Button
          variant="outline"
          className="text-slate-700"
          onClick={() => {
            setDraft({});
            onApply({});
          }}
        >
          Clear
        </Button>
      </div>
    </form>
  );
}

function RecordView({ record, onPivot }: { record: AuditRecord; onPivot: (f: Filters) => void }) {
  return (
    <details className="group py-2" data-testid="audit-record">
      <summary className="cursor-pointer list-none">
        <span className="flex flex-wrap items-center justify-between gap-2">
          <span className="min-w-0 break-all font-mono text-sm text-slate-900">{record.action}</span>
          <StatusBadge status={record.outcome} className={OUTCOME_STYLE[record.outcome] ?? ""} />
        </span>
        <span className="mt-0.5 block break-words text-xs text-slate-500">
          #{record.sequence} · {when(record.occurred_at)} · {record.actor_type} {record.actor_id}
          {record.resource_id && ` → ${record.resource_type} ${record.resource_id}`}
        </span>
      </summary>
      <dl className="mt-2 grid gap-x-4 gap-y-1 rounded-md bg-slate-50 p-3 text-sm sm:grid-cols-[auto_1fr]">
        <dt className="text-slate-500">Service</dt>
        <dd>{record.producer}</dd>
        <dt className="text-slate-500">Actor</dt>
        <dd className="break-all">
          {record.actor_type} {record.actor_id}
          {record.on_behalf_of && `, on behalf of ${record.on_behalf_of}`}
        </dd>
        <dt className="text-slate-500">Resource</dt>
        <dd className="break-all">{record.resource_id ? `${record.resource_type} ${record.resource_id}` : "—"}</dd>
        <dt className="text-slate-500">Request</dt>
        <dd className="break-all font-mono text-xs">{record.correlation_id || "—"}</dd>
        <dt className="text-slate-500">Recorded</dt>
        <dd>{when(record.recorded_at)}</dd>
        <dt className="text-slate-500">Event</dt>
        <dd className="break-all font-mono text-xs">{record.event_id}</dd>
        <dt className="text-slate-500">Hash</dt>
        <dd className="break-all font-mono text-xs" title={record.record_hash}>
          {short(record.record_hash)}, after {short(record.previous_hash)}
        </dd>
      </dl>
      <div className="mt-2 flex flex-wrap gap-2 text-sm">
        <Button variant="outline" className="text-brand-primary" onClick={() => onPivot({ actor_id: record.actor_id })}>
          Same actor
        </Button>
        {record.resource_id && (
          <Button
            variant="outline"
            className="text-brand-primary"
            onClick={() => onPivot({ resource_type: record.resource_type, resource_id: record.resource_id })}
          >
            Same resource
          </Button>
        )}
        {record.correlation_id && (
          <Button variant="outline" className="text-brand-primary" onClick={() => onPivot({ correlation_id: record.correlation_id })}>
            Same request
          </Button>
        )}
      </div>
    </details>
  );
}

/** One filtered listing, page by page. Keyed by the address, so new filters start at page one. */
function Records({
  api,
  filters,
  newestFirst,
  onPivot,
}: {
  api: AuditApi;
  filters: Filters;
  newestFirst: boolean;
  onPivot: (f: Filters) => void;
}) {
  const [pages, setPages] = useState<string[]>([""]);
  const load = useCallback(
    () => Promise.all(pages.map((token) => api.records(filters, newestFirst, token))),
    [api, filters, newestFirst, pages],
  );
  const [state] = useLoad(load);
  return (
    <Loading state={state}>
      {(loaded) => {
        const records = loaded.flatMap((page) => page.records);
        const more = loaded[loaded.length - 1]?.next_page_token ?? "";
        if (records.length === 0) return <p className="text-sm text-slate-500">No records match.</p>;
        return (
          <div>
            <ul className="divide-y divide-slate-100" aria-label="Audit records" data-testid="audit-records">
              {records.map((record) => (
                <li key={record.sequence}>
                  <RecordView record={record} onPivot={onPivot} />
                </li>
              ))}
            </ul>
            {more && (
              <Button variant="outline" className="mt-3 text-brand-primary" onClick={() => setPages([...pages, more])}>
                Show more
              </Button>
            )}
          </div>
        );
      }}
    </Loading>
  );
}

function Trail({ api }: { api: AuditApi }) {
  const [params, setParams] = useSearchParams();
  const key = params.toString();
  const applied = useMemo(() => filtersFrom(new URLSearchParams(key)), [key]);
  const newestFirst = params.get("order") !== "oldest";
  const apply = (filters: Filters) => {
    const next = new URLSearchParams();
    if (!newestFirst) next.set("order", "oldest");
    for (const [name, value] of Object.entries(filters)) if (value) next.set(name, value);
    setParams(next);
  };
  return (
    <div className="space-y-4">
      <Panel title="Filters">
        <FilterForm key={key} applied={applied} onApply={apply} />
      </Panel>
      <Panel
        title="Records"
        actions={
          <select
            aria-label="Order"
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
            value={newestFirst ? "newest" : "oldest"}
            onChange={(e) => {
              const next = new URLSearchParams(params);
              if (e.target.value === "oldest") next.set("order", "oldest");
              else next.delete("order");
              setParams(next);
            }}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
        }
      >
        <Records key={key} api={api} filters={applied} newestFirst={newestFirst} onPivot={apply} />
      </Panel>
    </div>
  );
}

function ChainPanel({ api }: { api: AuditApi }) {
  const [check, setCheck] = useState<ChainCheck | "busy" | "failed" | null>(null);
  return (
    <Panel title="The hash chain">
      <div className="space-y-2 text-sm">
        <p className="text-slate-600">
          Each record holds the hash of the one before it, so a changed or removed record breaks the chain from there on.
        </p>
        <Button
          variant="outline"
          className="text-brand-primary"
          disabled={check === "busy"}
          onClick={() => {
            setCheck("busy");
            api.verify().then(setCheck, () => setCheck("failed"));
          }}
        >
          Check the chain
        </Button>
        <div role="status" data-testid="chain-check">
          {check === "busy" && <p className="text-slate-500">Checking every record…</p>}
          {check === "failed" && <p className="text-red-800">The chain could not be checked right now. Try again in a moment.</p>}
          {typeof check === "object" && check !== null && (
            <p className={check.valid ? "text-emerald-800" : "font-medium text-red-800"}>
              {check.valid
                ? `Intact: ${check.records_checked.toLocaleString()} records checked.`
                : `Broken at record #${check.first_invalid_sequence}, after ${check.records_checked.toLocaleString()} intact records.`}
            </p>
          )}
        </div>
      </div>
    </Panel>
  );
}

function Deliveries({ api }: { api: AuditApi }) {
  const [caseId, setCaseId] = useState("");
  const [status, setStatus] = useState("");
  const [asked, setAsked] = useState({ caseId: "", status: "" });
  const load = useCallback(() => api.deliveries(asked.caseId, asked.status), [api, asked]);
  const [state] = useLoad(load);
  return (
    <Panel title="Notification deliveries">
      <form
        className="mb-3 grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end"
        aria-label="Filter deliveries"
        onSubmit={(event) => {
          event.preventDefault();
          setAsked({ caseId: caseId.trim(), status });
        }}
      >
        <div>
          <label className={LABEL} htmlFor="delivery-case">
            Case
          </label>
          <input id="delivery-case" className={INPUT} maxLength={64} value={caseId} onChange={(e) => setCaseId(e.target.value)} />
        </div>
        <div>
          <label className={LABEL} htmlFor="delivery-status">
            Status
          </label>
          <select id="delivery-status" className={INPUT} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Any</option>
            {["delivered", "pending", "recorded", "failed"].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" disabled={caseId.trim() !== "" && !/^[A-Za-z0-9_-]{1,64}$/.test(caseId.trim())}>
          Show
        </Button>
      </form>
      <Loading state={state}>
        {(deliveries) =>
          deliveries.length === 0 ? (
            <p className="text-sm text-slate-500">No deliveries match.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[32rem] text-left text-sm" data-testid="deliveries">
                <thead className="text-xs text-slate-500">
                  <tr>
                    <th className="py-1 font-medium">When</th>
                    <th className="py-1 font-medium">Kind</th>
                    <th className="py-1 font-medium">To</th>
                    <th className="py-1 font-medium">Channel</th>
                    <th className="py-1 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {deliveries.map((d) => (
                    <tr key={`${d.notification_id}-${d.channel}`} className="border-t border-slate-100 align-top">
                      <td className="py-1 pr-2 text-xs">{when(d.updated_at)}</td>
                      <td className="py-1 pr-2">{d.kind}</td>
                      <td className="break-all py-1 pr-2 font-mono text-xs">{d.user_id}</td>
                      <td className="py-1 pr-2">{d.channel === "email" ? "Email" : "In app"}</td>
                      <td className="py-1">
                        {d.status}
                        {d.attempts > 1 && ` (${d.attempts} tries)`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loading>
    </Panel>
  );
}

export function AuditScreen({ token }: { token: string }) {
  const api = useMemo(() => auditApi(token), [token]);
  const [params] = useSearchParams();
  const tab = params.get("tab") === "deliveries" ? "deliveries" : "trail";
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Audit explorer</h2>
      <nav aria-label="Audit" className="flex flex-wrap gap-2">
        {(
          [
            ["trail", "Audit trail", "?"],
            ["deliveries", "Notification deliveries", "?tab=deliveries"],
          ] as const
        ).map(([id, label, to]) => (
          <Link
            key={id}
            to={to}
            aria-current={tab === id ? "page" : undefined}
            className={`rounded-md px-3 py-1 text-sm ring-1 ${tab === id ? "bg-sky-50 ring-sky-300" : "ring-slate-200 hover:bg-slate-50"}`}
          >
            {label}
          </Link>
        ))}
      </nav>
      {tab === "trail" ? (
        <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
          <Trail api={api} />
          <div>
            <ChainPanel api={api} />
          </div>
        </div>
      ) : (
        <Deliveries api={api} />
      )}
    </section>
  );
}
