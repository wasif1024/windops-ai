import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { fitsText, type PartsApi, partsApi, type Search, type Stock } from "../api";

function stockStyle(stock: Stock): string {
  return stock.available > 0
    ? "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200"
    : "bg-amber-50 text-amber-900 ring-1 ring-amber-200";
}

/** Find parts by words or component, or those that fit a turbine, with its store's stock. */
export function PartsLookup({ api }: { api: PartsApi }) {
  const [params, setParams] = useSearchParams();
  const search: Search = useMemo(
    () => ({ turbine_id: params.get("turbine") ?? "", component_kind: params.get("kind") ?? "", query: params.get("q") ?? "" }),
    [params],
  );
  const [form, setForm] = useState(search);
  const load = useCallback(() => api.search(search), [api, search]);
  const [state] = useLoad(load);
  const back = params.toString();
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Parts lookup</h2>
      <Panel title="Find parts">
        <form
          className="grid gap-3 sm:grid-cols-[1fr_1fr_1fr_auto] sm:items-end"
          aria-label="Find parts"
          onSubmit={(event) => {
            event.preventDefault();
            const next = new URLSearchParams();
            if (form.turbine_id.trim()) next.set("turbine", form.turbine_id.trim());
            if (form.component_kind.trim()) next.set("kind", form.component_kind.trim());
            if (form.query.trim()) next.set("q", form.query.trim());
            setParams(next);
          }}
        >
          <div className="space-y-1">
            <label className={LABEL} htmlFor="parts-turbine">
              Fits turbine
            </label>
            <input
              id="parts-turbine"
              className={INPUT}
              placeholder="dogger-east-t01"
              value={form.turbine_id}
              maxLength={64}
              onChange={(e) => setForm({ ...form, turbine_id: e.target.value })}
            />
          </div>
          <div className="space-y-1">
            <label className={LABEL} htmlFor="parts-kind">
              Component
            </label>
            <input
              id="parts-kind"
              className={INPUT}
              placeholder="gearbox"
              value={form.component_kind}
              maxLength={64}
              onChange={(e) => setForm({ ...form, component_kind: e.target.value })}
            />
          </div>
          <div className="space-y-1">
            <label className={LABEL} htmlFor="parts-words">
              Words
            </label>
            <input
              id="parts-words"
              className={INPUT}
              value={form.query}
              maxLength={200}
              onChange={(e) => setForm({ ...form, query: e.target.value })}
            />
          </div>
          <Button type="submit">Search</Button>
        </form>
      </Panel>
      <Panel title="Parts">
        <Loading state={state}>
          {(found) => (
            <>
              {found.model && (
                <p className="mb-3 break-words text-sm text-slate-600" data-testid="parts-context">
                  Parts that fit {found.turbine_id}, a {found.model.manufacturer} {found.model.model}, with stock in the{" "}
                  {found.wind_farm_id} store.
                </p>
              )}
              {found.parts.length === 0 ? (
                <p className="text-slate-500" data-testid="no-parts">
                  No parts match.
                </p>
              ) : (
                <ul className="space-y-2" aria-label="Parts">
                  {found.parts.map(({ part, stock }) => (
                    <li key={part.part_number}>
                      <Link
                        to={`?part=${encodeURIComponent(part.part_number)}${back ? `&${back}` : ""}`}
                        className="flex flex-col gap-1 rounded-lg border border-slate-200 bg-white p-3 hover:border-brand-primary hover:shadow-sm sm:flex-row sm:items-center sm:gap-3"
                        data-testid={`part-${part.part_number}`}
                      >
                        <span className="min-w-0 flex-1">
                          <span className="block break-words font-medium text-slate-900">{part.name}</span>
                          <span className="block break-all font-mono text-xs text-slate-500">{part.part_number}</span>
                        </span>
                        <span className="text-xs text-slate-500 sm:w-32">{titleCase(part.component_kind)}</span>
                        {stock && <StatusBadge status={`${stock.available} available`} className={stockStyle(stock)} />}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </Loading>
        <p className="mt-3 text-xs text-slate-500">Looking up a part holds nothing. Only a person reserves parts.</p>
      </Panel>
    </section>
  );
}

/** One part: what it is, the models it fits, and its stock in each store the user may read. */
export function PartPage({ api, partNumber }: { api: PartsApi; partNumber: string }) {
  const [params] = useSearchParams();
  const load = useCallback(() => api.detail(partNumber), [api, partNumber]);
  const [state] = useLoad(load);
  const back = new URLSearchParams(params);
  back.delete("part");
  return (
    <section className="space-y-4">
      <Link to={`?${back.toString()}`} className="text-sm text-brand-primary hover:underline">
        ← Parts lookup
      </Link>
      <Loading state={state}>
        {({ part, stock }) => (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="break-words text-xl font-semibold">{part.name}</h2>
              {part.retired && <StatusBadge status="Retired" className="bg-red-50 text-red-800 ring-1 ring-red-200" />}
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title="Part">
                <dl className="text-sm">
                  {[
                    ["Part number", part.part_number],
                    ["Component", titleCase(part.component_kind)],
                    ["Unit", part.unit],
                    ["Fits", fitsText(part)],
                  ].map(([label, value]) => (
                    <div key={label} className="flex justify-between gap-4 border-b border-slate-100 py-1.5">
                      <dt className="shrink-0 text-slate-500">{label}</dt>
                      <dd className="min-w-0 break-words text-right font-medium text-slate-800">{value}</dd>
                    </div>
                  ))}
                </dl>
                {part.description && <p className="mt-3 break-words text-sm text-slate-700">{part.description}</p>}
              </Panel>
              <Panel title="Stock">
                {stock.length === 0 ? (
                  <p className="text-sm text-slate-500">No store you may read holds it.</p>
                ) : (
                  <ul className="space-y-2" aria-label="Stores">
                    {stock.map((store) => (
                      <li key={store.wind_farm_id} className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-2 text-sm">
                        <span className="font-medium text-slate-800">{store.wind_farm_id}</span>
                        <span className="text-slate-500">
                          {store.on_hand} on hand · {store.reserved} reserved
                        </span>
                        <StatusBadge status={`${store.available} available`} className={stockStyle(store)} />
                        <span className="w-full text-xs text-slate-400">Updated {when(store.updated_at)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>
            </div>
          </>
        )}
      </Loading>
    </section>
  );
}

export function PartsScreen({ token }: { token: string }) {
  const api = useMemo(() => partsApi(token), [token]);
  const [params] = useSearchParams();
  const part = params.get("part");
  return part ? <PartPage api={api} partNumber={part} /> : <PartsLookup api={api} />;
}
