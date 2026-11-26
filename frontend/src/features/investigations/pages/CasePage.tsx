// One case: where it is, its investigation live, its findings and their reviews, the check after
// its work, and everything that happened to it (SRS 4.11, M11.1).
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import {
  CASE_STYLE,
  type CaseDetail,
  type CaseEvent,
  type InvestigationApi,
  type ProgressEvent,
  SEVERITY_STYLE,
  type Verification,
} from "../api";
import { follow } from "../stream";
import { Findings } from "./Findings";

const LIVE = ["open", "investigating"];

/** What a progress event says, in a line. */
export function describe(event: ProgressEvent): string {
  if (event.kind === "step") return `Step ${event.position} of ${event.total_steps}: ${titleCase(event.step)}`;
  if (event.kind === "case_status") return `The case is ${titleCase(event.status).toLowerCase()}`;
  if (event.kind === "run_ended") return `The run ended: ${titleCase(event.status).toLowerCase()}`;
  const said = [titleCase(event.kind), event.status && titleCase(event.status).toLowerCase(), event.note]
    .filter(Boolean)
    .join(": ");
  return said;
}

function Progress({ api, caseId, live, onFinished }: { api: InvestigationApi; caseId: string; live: boolean; onFinished: () => void }) {
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    const stop = new AbortController();
    // Read again from the start when the case moves on: each event is kept once, by its sequence.
    const add = (event: ProgressEvent) =>
      setEvents((seen) => (seen.some((known) => known.sequence === event.sequence) ? seen : [...seen, event]));
    follow(caseId, api.token, add, stop.signal)
      .then(() => {
        if (!stop.signal.aborted && live) onFinished();
      })
      .catch(() => setProblem("Its progress cannot be shown right now."));
    return () => stop.abort();
  }, [api.token, caseId, live, onFinished]);
  const last = events.at(-1);
  const step = [...events].reverse().find((event) => event.kind === "step");
  return (
    <div className="space-y-3" data-testid="progress">
      {step && step.total_steps > 0 && (
        <div>
          <div className="h-2 w-full overflow-hidden rounded bg-slate-100">
            <div
              className="h-2 bg-brand-primary"
              style={{ width: `${Math.round((100 * step.position) / step.total_steps)}%` }}
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={step.total_steps}
              aria-valuenow={step.position}
              aria-label="Investigation steps"
            />
          </div>
          <p className="mt-1 text-xs text-slate-500">
            Step {step.position} of {step.total_steps}
            {live && last && " · following live"}
          </p>
        </div>
      )}
      {problem && <p role="alert" className="text-sm text-red-800">{problem}</p>}
      {events.length === 0 ? (
        <p className="text-sm text-slate-500">{live ? "Waiting for its first step…" : "No investigation progress recorded."}</p>
      ) : (
        <ol className="max-h-72 space-y-1 overflow-y-auto text-sm" aria-label="Progress">
          {events.map((event) => (
            <li key={event.sequence} className="flex gap-3 border-b border-slate-100 py-1">
              <span className="w-8 shrink-0 text-right text-xs text-slate-400">{event.sequence}</span>
              <span className="min-w-0 break-words text-slate-800">{describe(event)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function Timeline({ history }: { history: CaseEvent[] }) {
  return (
    <ol className="space-y-2 text-sm" aria-label="Timeline">
      {history.map((event, index) => (
        <li key={`${index}-${event.action}`} className="border-l-2 border-slate-200 pl-3">
          <p className="font-medium text-slate-800">{titleCase(event.action)}</p>
          <p className="break-words text-xs text-slate-500">
            {when(event.at)} · {event.actor_id}
          </p>
          {event.note && <p className="break-words text-xs text-slate-600">{event.note}</p>}
        </li>
      ))}
    </ol>
  );
}

const OUTCOME_STYLE: Record<string, string> = {
  resolved: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  not_resolved: "bg-red-50 text-red-800 ring-1 ring-red-200",
  inconclusive: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
};

function figure(value: number | null, unit: string): string {
  return value === null ? "—" : `${Math.round(value * 10) / 10} ${unit}`;
}

function Answer({ api, detail, onDone }: { api: InvestigationApi; detail: CaseDetail; onDone: () => void }) {
  const change = useChange();
  const [reason, setReason] = useState("");
  const busy = change.state.kind === "busy";
  const caseId = detail.investigation.case_id;
  const send = async (decision: "close" | "reopen") => {
    if (await change.run((key) => api.answer(caseId, key, decision, reason.trim(), detail.version))) onDone();
  };
  return (
    <div className="space-y-2">
      <label className={LABEL} htmlFor="verification-reason">
        Reason (needed to reopen)
      </label>
      <textarea id="verification-reason" className={INPUT} rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy} onClick={() => void send("close")}>
          Close the case
        </Button>
        <Button variant="outline" className="text-slate-700" disabled={busy || !reason.trim()} onClick={() => void send("reopen")}>
          Reopen to investigate again
        </Button>
      </div>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </div>
  );
}

function VerificationView({ verification }: { verification: Verification }) {
  return (
    <div className="space-y-3" data-testid="verification">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="break-all">
          Work order {verification.work_order_id} · version {verification.proposal_version}
        </span>
        {verification.outcome ? (
          <StatusBadge status={titleCase(verification.outcome)} className={OUTCOME_STYLE[verification.outcome] ?? ""} />
        ) : (
          <StatusBadge status={titleCase(verification.status)} className="bg-teal-50 text-teal-800 ring-1 ring-teal-200" />
        )}
      </div>
      <p className="text-xs text-slate-500">
        Completed by {verification.completed_by} {when(verification.completed_at)} · watched from {when(verification.window_start)} to{" "}
        {when(verification.window_end)}
      </p>
      {verification.note && <p className="break-words text-sm text-slate-700">{verification.note}</p>}
      {verification.metrics.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[22rem] text-left text-sm">
            <caption className="sr-only">Before and after the work</caption>
            <thead className="text-xs text-slate-500">
              <tr>
                <th className="py-1 font-medium">Metric</th>
                <th className="py-1 font-medium">Mean before</th>
                <th className="py-1 font-medium">Mean after</th>
                <th className="py-1 font-medium">Max after</th>
              </tr>
            </thead>
            <tbody>
              {verification.metrics.map((m) => (
                <tr key={m.metric} className="border-t border-slate-100">
                  <td className="py-1 pr-2">{titleCase(m.metric.replace(/^METRIC_/, "").toLowerCase())}</td>
                  <td className="py-1 pr-2">{figure(m.before_mean, m.unit)}</td>
                  <td className="py-1 pr-2">{figure(m.after_mean, m.unit)}</td>
                  <td className="py-1">{figure(m.after_maximum, m.unit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {verification.rules.length > 0 && (
        <ul className="space-y-1 text-xs" aria-label="Fault rules after the work">
          {verification.rules.map((rule) => (
            <li key={rule.rule_id} className="break-words text-slate-600">
              {rule.rule_id} v{rule.version}: {rule.severity}
              {rule.counts ? "" : " (not one of the case's rules)"} · {rule.summary}
            </li>
          ))}
        </ul>
      )}
      {verification.review && (
        <p className="break-words text-sm text-slate-700">
          {titleCase(verification.review)} by {verification.reviewed_by} {when(verification.reviewed_at)}
          {verification.review_reason && `: “${verification.review_reason}”`}
        </p>
      )}
    </div>
  );
}

export function CasePage({
  api,
  caseId,
  canReview,
}: {
  api: InvestigationApi;
  caseId: string;
  canReview: boolean;
}) {
  const loadDetail = useCallback(() => api.detail(caseId), [api, caseId]);
  const loadReport = useCallback(() => api.report(caseId), [api, caseId]);
  const [detail, reloadDetail] = useLoad(loadDetail);
  const [report, reloadReport] = useLoad(loadReport);
  const refresh = useCallback(() => {
    reloadDetail();
    reloadReport();
  }, [reloadDetail, reloadReport]);
  return (
    <section className="space-y-4">
      <Link to="?" className="text-sm text-brand-primary hover:underline">
        ← Investigations
      </Link>
      <Loading state={detail}>
        {(data) => {
          const c = data.investigation;
          const [verification] = data.verifications;
          return (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="break-words text-xl font-semibold">{c.title}</h2>
                <StatusBadge status={titleCase(c.status)} className={CASE_STYLE[c.status]} />
                <StatusBadge status={titleCase(c.severity)} className={SEVERITY_STYLE[c.severity] ?? ""} />
              </div>
              <p className="break-all text-sm text-slate-500">
                {c.case_id} · {c.turbine_id} · {data.origin === "detection" ? "from detections" : `opened by ${data.created_by}`} ·{" "}
                {when(c.created_at)}
              </p>
              {data.stale_rule && (
                <p role="note" className="break-words rounded-md border border-orange-200 bg-orange-50 p-3 text-sm text-orange-900">
                  Its evidence is stale ({data.stale_rule}): {data.stale_detail}. It cannot be approved until it is re-evaluated.
                </p>
              )}
              {c.status === "verified" && verification && (
                <Panel title="Verification">
                  <VerificationView verification={verification} />
                  {canReview && (
                    <div className="mt-4 border-t border-slate-100 pt-4">
                      <Answer api={api} detail={data} onDone={refresh} />
                    </div>
                  )}
                </Panel>
              )}
              <div className="grid gap-4 lg:grid-cols-3">
                <div className="space-y-4 lg:col-span-2">
                  <Panel title="Findings">
                    <Loading state={report}>
                      {(found) => <Findings api={api} caseId={caseId} report={found} canReview={canReview} onReviewed={reloadReport} />}
                    </Loading>
                  </Panel>
                  {c.status !== "verified" && verification && (
                    <Panel title="Verification">
                      <VerificationView verification={verification} />
                    </Panel>
                  )}
                </div>
                <div className="space-y-4">
                  <Panel title="Live progress">
                    <Progress key={caseId} api={api} caseId={caseId} live={LIVE.includes(c.status)} onFinished={refresh} />
                  </Panel>
                  <Panel title="Evidence">
                    <dl className="space-y-1 text-sm">
                      <dt className="text-slate-500">Snapshot</dt>
                      <dd className="break-all font-mono text-xs">
                        {data.evidence_snapshot_id || "Not frozen yet"}
                        {data.evidence_version > 0 && ` (version ${data.evidence_version})`}
                      </dd>
                      <dt className="text-slate-500">Detections</dt>
                      <dd className="break-words">
                        {data.detections.map((d) => `${d.rule_id} (${d.severity})`).join(", ") || "None: opened by a person"}
                      </dd>
                    </dl>
                    {data.evidence_snapshot_id && (
                      <Link
                        to={`?case=${encodeURIComponent(c.case_id)}&evidence=${encodeURIComponent(data.evidence_snapshot_id)}`}
                        className="mt-3 inline-block text-sm text-brand-primary hover:underline"
                      >
                        Open the evidence
                      </Link>
                    )}
                  </Panel>
                  <Panel title="Timeline">
                    <Timeline history={data.history} />
                  </Panel>
                </div>
              </div>
            </>
          );
        }}
      </Loading>
    </section>
  );
}
