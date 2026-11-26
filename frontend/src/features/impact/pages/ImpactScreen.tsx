// The production impact view (SRS 4.11, M11.3): a case's estimates, each figure with its range,
// the inputs and assumptions it rests on, and a form to estimate an action. Every figure is worked
// out by the Production Impact Service, never judged (FR-IMP-004).
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { investigationApi } from "../../investigations/api";
import { ACTIONS } from "../../maintenance/api";
import { amount, figureName, type ImpactApi, impactApi, type ImpactResult, placeIn } from "../api";

function FigureRow({ figure }: { figure: ImpactResult["figures"][number] }) {
  const place = placeIn(figure);
  return (
    <li className="space-y-1" data-testid={`figure-${figure.name}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm text-slate-700">{figureName(figure.name)}</span>
        <span className="font-semibold text-slate-900">{amount(figure.value, figure.unit)}</span>
      </div>
      <div className="relative h-2 w-full rounded bg-slate-100" aria-hidden="true">
        <div className="absolute inset-y-0 rounded bg-sky-200" style={{ left: `${place.low}%`, right: "0%" }} />
        <div className="absolute inset-y-0 w-1 -translate-x-1/2 rounded bg-sky-700" style={{ left: `${place.value}%` }} />
      </div>
      <p className="text-xs text-slate-500">
        Range {amount(figure.low, figure.unit)} to {amount(figure.high, figure.unit)}
      </p>
    </li>
  );
}

function text(value: unknown): string {
  return typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "—");
}

function ResultView({ result }: { result: ImpactResult }) {
  return (
    <div className="space-y-4" data-testid="impact-result">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge status={titleCase(result.action)} className="bg-slate-100 text-slate-700 ring-1 ring-slate-200" />
        <span>{result.downtime_hours} hours down</span>
        {result.verified ? (
          <StatusBadge status="Hash matches" className="bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200" />
        ) : (
          <StatusBadge status="Hash does not match" className="bg-red-50 text-red-800 ring-1 ring-red-200" />
        )}
      </div>
      <p className="break-words text-xs text-slate-500">
        {result.result_id} · {result.formula_version} · estimated by {result.estimated_by} {when(result.estimated_at)}
        {result.window_start && ` · downtime from ${when(result.window_start)} to ${when(result.window_end)}`}
      </p>
      <ul className="space-y-4" aria-label="Figures">
        {result.figures.map((figure) => (
          <FigureRow key={figure.name} figure={figure} />
        ))}
      </ul>
      <div>
        <h4 className="mb-1 text-sm font-semibold text-slate-700">Assumptions</h4>
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-700" aria-label="Assumptions">
          {result.assumptions.map((assumption) => (
            <li key={assumption} className="break-words">
              {assumption}
            </li>
          ))}
        </ul>
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-brand-primary">Inputs ({result.inputs.length})</summary>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[24rem] text-left text-xs">
            <thead className="text-slate-500">
              <tr>
                <th className="py-1 font-medium">Input</th>
                <th className="py-1 font-medium">Value</th>
                <th className="py-1 font-medium">From</th>
              </tr>
            </thead>
            <tbody>
              {result.inputs.map((input) => (
                <tr key={input.name} className="border-t border-slate-100 align-top">
                  <td className="py-1 pr-2 font-mono">{input.name}</td>
                  <td className="break-all py-1 pr-2">{text(input.value)}</td>
                  <td className="break-all py-1">
                    {input.source}
                    {input.source_ref && ` · ${input.source_ref}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

function EstimateForm({ api, caseId, onMade }: { api: ImpactApi; caseId: string; onMade: (id: string) => void }) {
  const change = useChange();
  const [action, setAction] = useState("inspection");
  const [hours, setHours] = useState("4");
  const [start, setStart] = useState("");
  const downtime = Number(hours);
  const fine = downtime > 0 && downtime <= 720;
  return (
    <form
      className="grid gap-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end"
      aria-label="Estimate an action"
      onSubmit={(event) => {
        event.preventDefault();
        const body = { case_id: caseId, action, downtime_hours: downtime, ...(start ? { planned_start: new Date(start).toISOString() } : {}) };
        void change.run((key) => api.estimate(key, body)).then((made) => made && onMade(made.result_id));
      }}
    >
      <div>
        <label className={LABEL} htmlFor="impact-action">
          Action
        </label>
        <select id="impact-action" className={INPUT} value={action} onChange={(e) => setAction(e.target.value)}>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>
              {titleCase(a)}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label className={LABEL} htmlFor="impact-hours">
          Downtime, hours
        </label>
        <input id="impact-hours" type="number" min={0.5} max={720} step={0.5} className={INPUT} value={hours} onChange={(e) => setHours(e.target.value)} />
      </div>
      <div>
        <label className={LABEL} htmlFor="impact-start">
          Starting (optional)
        </label>
        <input id="impact-start" type="datetime-local" className={INPUT} value={start} onChange={(e) => setStart(e.target.value)} />
      </div>
      <Button type="submit" disabled={change.state.kind === "busy" || !fine}>
        Estimate
      </Button>
      <p className="text-xs text-slate-500 sm:col-span-4">
        With a start, the lost energy follows the forecast hour by hour; without one, the observed wind held steady.
      </p>
      <div className="sm:col-span-4">
        <ChangeStatus state={change.state} onDismiss={change.reset} />
      </div>
    </form>
  );
}

function CaseImpact({ api, caseId }: { api: ImpactApi; caseId: string }) {
  const [chosen, setChosen] = useState<string | null>(null);
  const load = useCallback(() => api.list(caseId), [api, caseId]);
  const [state, reload] = useLoad(load);
  return (
    <div className="space-y-4">
      <Panel title="Estimate an action">
        <EstimateForm
          api={api}
          caseId={caseId}
          onMade={(id) => {
            setChosen(id);
            reload();
          }}
        />
      </Panel>
      <Loading state={state}>
        {(results) => {
          if (results.length === 0) return <p className="text-sm text-slate-500" data-testid="no-estimates">No estimates for this case yet.</p>;
          const shown = results.find((r) => r.result_id === chosen) ?? results[0];
          return (
            <div className="grid gap-4 lg:grid-cols-3">
              <Panel title="Estimates">
                <ul className="space-y-1 text-sm" aria-label="Estimates">
                  {results.map((r) => (
                    <li key={r.result_id}>
                      <button
                        type="button"
                        aria-pressed={r.result_id === shown?.result_id}
                        onClick={() => setChosen(r.result_id)}
                        className={`w-full rounded-md px-2 py-1 text-left ${r.result_id === shown?.result_id ? "bg-sky-50 ring-1 ring-sky-200" : "hover:bg-slate-50"}`}
                      >
                        <span className="block">
                          {titleCase(r.action)} · {r.downtime_hours} h
                        </span>
                        <span className="block text-xs text-slate-500">{when(r.estimated_at)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </Panel>
              <div className="lg:col-span-2">{shown && <Panel title="Estimate">{<ResultView result={shown} />}</Panel>}</div>
            </div>
          );
        }}
      </Loading>
    </div>
  );
}

export function ImpactScreen({ token }: { token: string }) {
  const api = useMemo(() => impactApi(token), [token]);
  const cases = useMemo(() => investigationApi(token), [token]);
  const [params, setParams] = useSearchParams();
  const caseId = params.get("case") ?? "";
  const loadCases = useCallback(() => cases.list(false), [cases]);
  const [list] = useLoad(loadCases);
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Production impact</h2>
      <Panel title="Case">
        <Loading state={list}>
          {(items) => (
            <div className="space-y-2">
              <label className={LABEL} htmlFor="impact-case">
                Active case
              </label>
              <select id="impact-case" className={INPUT} value={caseId} onChange={(e) => setParams(e.target.value ? { case: e.target.value } : {})}>
                <option value="">Choose…</option>
                {items.map((c) => (
                  <option key={c.case_id} value={c.case_id}>
                    {c.title} · {c.turbine_id}
                  </option>
                ))}
              </select>
              {caseId && (
                <Link to={`/screens/investigations?case=${encodeURIComponent(caseId)}`} className="inline-block text-sm text-brand-primary hover:underline">
                  Open the case
                </Link>
              )}
            </div>
          )}
        </Loading>
      </Panel>
      {caseId ? <CaseImpact key={caseId} api={api} caseId={caseId} /> : <p className="text-sm text-slate-500">Choose a case to see its impact.</p>}
    </section>
  );
}
