// The investigation list, and one case when the address names it (SRS 4.11, M11.1).
import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { fleetApi } from "../../fleet/api";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { CASE_STYLE, type Investigation, type InvestigationApi, investigationApi, SEVERITY_STYLE } from "../api";
import { CasePage } from "./CasePage";
import { EvidenceViewer } from "./EvidenceViewer";

const SEVERITIES = ["low", "medium", "high", "critical"];

function Row({ item }: { item: Investigation }) {
  return (
    <li>
      <Link
        to={`?case=${encodeURIComponent(item.case_id)}`}
        className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-3 hover:border-brand-primary hover:shadow-sm sm:flex-row sm:items-center"
        data-testid={`case-${item.case_id}`}
      >
        <span className="flex shrink-0 flex-wrap items-center gap-2 sm:w-56">
          <StatusBadge status={titleCase(item.status)} className={CASE_STYLE[item.status]} />
          <StatusBadge status={titleCase(item.severity)} className={SEVERITY_STYLE[item.severity] ?? ""} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block break-words font-medium text-slate-900">{item.title}</span>
          <span className="block break-all text-xs text-slate-500">
            {item.turbine_id} · {item.case_id}
          </span>
        </span>
        <span className="shrink-0 text-xs text-slate-500 sm:w-40 sm:text-right">{when(item.updated_at)}</span>
      </Link>
    </li>
  );
}

/** A person opens a case and investigates it: the case's ID comes back at once (API-003). */
function StartForm({ api, token }: { api: InvestigationApi; token: string }) {
  const navigate = useNavigate();
  const fleet = useMemo(() => fleetApi(token), [token]);
  const loadFleet = useCallback(() => fleet.fleet(), [fleet]);
  const [turbines] = useLoad(loadFleet);
  const change = useChange();
  const [turbine, setTurbine] = useState("");
  const [title, setTitle] = useState("");
  const [severity, setSeverity] = useState("medium");
  return (
    <Loading state={turbines}>
      {(data) => (
        <form
          className="grid gap-3 sm:grid-cols-[1fr_2fr_auto_auto] sm:items-end"
          aria-label="Investigate a turbine"
          onSubmit={(event) => {
            event.preventDefault();
            void change.run((key) => api.start(key, turbine, title.trim(), severity)).then((made) => {
              if (made) navigate(`?case=${encodeURIComponent(made.case_id)}`);
            });
          }}
        >
          <div>
            <label className={LABEL} htmlFor="investigate-turbine">
              Turbine
            </label>
            <select id="investigate-turbine" className={INPUT} value={turbine} onChange={(e) => setTurbine(e.target.value)}>
              <option value="">Choose…</option>
              {data.wind_farms.map((farm) => (
                <optgroup key={farm.wind_farm_id} label={farm.name}>
                  {farm.turbines.map((t) => (
                    <option key={t.turbine_id} value={t.turbine_id}>
                      {t.name} ({t.turbine_id})
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
          </div>
          <div>
            <label className={LABEL} htmlFor="investigate-title">
              What to look into
            </label>
            <input id="investigate-title" className={INPUT} maxLength={500} value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div>
            <label className={LABEL} htmlFor="investigate-severity">
              Severity
            </label>
            <select id="investigate-severity" className={INPUT} value={severity} onChange={(e) => setSeverity(e.target.value)}>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {titleCase(s)}
                </option>
              ))}
            </select>
          </div>
          <Button type="submit" disabled={change.state.kind === "busy" || !turbine || !title.trim()}>
            Investigate
          </Button>
          <div className="sm:col-span-4">
            <ChangeStatus state={change.state} onDismiss={change.reset} />
          </div>
        </form>
      )}
    </Loading>
  );
}

export function InvestigationList({ api, token, canStart }: { api: InvestigationApi; token: string; canStart: boolean }) {
  const [every, setEvery] = useState(false);
  const load = useCallback(() => api.list(every), [api, every]);
  const [state] = useLoad(load);
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Investigations</h2>
      {canStart && (
        <Panel title="Investigate a turbine">
          <StartForm api={api} token={token} />
        </Panel>
      )}
      <Panel title="Cases">
        <div role="group" aria-label="Show" className="mb-4 flex flex-wrap gap-2">
          {[
            { id: false, label: "Active" },
            { id: true, label: "All" },
          ].map((option) => (
            <button
              key={option.label}
              type="button"
              aria-pressed={option.id === every}
              onClick={() => setEvery(option.id)}
              className={`rounded-full px-3 py-1 text-sm ring-1 ${
                option.id === every ? "bg-brand-primary text-white ring-brand-primary" : "bg-white text-slate-700 ring-slate-300"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
        <Loading state={state}>
          {(cases) =>
            cases.length === 0 ? (
              <p className="text-slate-500" data-testid="no-cases">
                No {every ? "" : "active "}cases.
              </p>
            ) : (
              <ul className="space-y-2" aria-label="Cases">
                {cases.map((item) => (
                  <Row key={item.case_id} item={item} />
                ))}
              </ul>
            )
          }
        </Loading>
      </Panel>
    </section>
  );
}

export function InvestigationScreen({ token, held }: { token: string; held: ReadonlySet<string> }) {
  const api = useMemo(() => investigationApi(token), [token]);
  const [params] = useSearchParams();
  const caseId = params.get("case");
  if (caseId && params.has("evidence")) {
    return (
      <EvidenceViewer
        api={api}
        caseId={caseId}
        snapshotId={params.get("evidence") || null}
        canFreeze={held.has("investigation.create")}
      />
    );
  }
  if (caseId) return <CasePage api={api} caseId={caseId} canReview={held.has("finding.review")} />;
  return <InvestigationList api={api} token={token} canStart={held.has("investigation.create")} />;
}
