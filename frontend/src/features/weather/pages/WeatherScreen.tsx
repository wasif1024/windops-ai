// The weather and source freshness panel (SRS 4.11, M11.3): a turbine's forecast over a window,
// and how fresh each source a decision rests on is. A forecast's age is set beside the limit the
// work-window rules hold it to, as a stale one blocks work at height (FR-POL-005).
import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useLoad } from "../../../api/useLoad";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { fleetApi, type TurbineSummary } from "../../fleet/api";
import { FreshnessBadge, StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { duration, type Forecast, STATE_STYLE, value, weatherApi } from "../api";

const WINDOWS = [
  { hours: 24, label: "24 hours" },
  { hours: 72, label: "3 days" },
  { hours: 168, label: "7 days" },
];
// How old a forecast the default work-window rules accept, as the Policy Service holds them.
export const FORECAST_LIMIT_SECONDS = 3 * 3600;

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-slate-100 py-1.5 text-sm">
      <dt className="shrink-0 text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words text-right font-medium text-slate-800">{children}</dd>
    </div>
  );
}

function Freshness({ forecast, turbine, now }: { forecast: Forecast | null; turbine: TurbineSummary | undefined; now: number }) {
  const withinLimit = forecast ? forecast.state !== "unavailable" && forecast.age_seconds <= FORECAST_LIMIT_SECONDS : false;
  return (
    <dl data-testid="freshness">
      <Fact label="Forecast">
        {forecast ? (
          <span className="inline-flex flex-wrap items-center justify-end gap-2">
            <StatusBadge status={titleCase(forecast.state)} className={STATE_STYLE[forecast.state]} />
            {forecast.state !== "unavailable" && <span>{duration(forecast.age_seconds)} old</span>}
          </span>
        ) : (
          "—"
        )}
      </Fact>
      <Fact label="For the work-window rules">
        {forecast ? (
          withinLimit ? (
            <span className="text-emerald-800">Within the {duration(FORECAST_LIMIT_SECONDS)} limit</span>
          ) : (
            <span className="text-red-800">Too old: work at height is blocked until it is fresh</span>
          )
        ) : (
          "—"
        )}
      </Fact>
      <Fact label="Telemetry">{turbine ? <FreshnessBadge latest={turbine.latest} now={now} /> : "—"}</Fact>
      {forecast?.note && <p className="pt-2 text-xs text-amber-900">{forecast.note}</p>}
    </dl>
  );
}

function Points({ forecast }: { forecast: Forecast }) {
  return (
    <div className="max-h-96 overflow-auto">
      <table className="w-full min-w-[30rem] text-left text-sm" aria-label="Forecast hours">
        <thead className="sticky top-0 bg-white text-xs text-slate-500">
          <tr>
            <th className="py-1 font-medium">Hour</th>
            <th className="py-1 font-medium">Wind at hub</th>
            <th className="py-1 font-medium">Gusts</th>
            <th className="py-1 font-medium">Rain</th>
            <th className="py-1 font-medium">Visibility</th>
          </tr>
        </thead>
        <tbody>
          {forecast.points.map((p) => (
            <tr key={p.valid_at} className="border-t border-slate-100">
              <td className="py-1 pr-2 text-slate-600">{p.valid_at ? new Date(p.valid_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "—"}</td>
              <td className="py-1 pr-2">{value(p.wind_speed_hub_mps, "m/s")}</td>
              <td className="py-1 pr-2">{value(p.wind_gusts_10m_mps, "m/s")}</td>
              <td className="py-1 pr-2">{value(p.precipitation_mm, "mm")}</td>
              <td className="py-1">{p.visibility_m === null ? "—" : `${(p.visibility_m / 1000).toFixed(1)} km`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ForecastPanels({ forecast, turbine, now }: { forecast: Forecast; turbine: TurbineSummary | undefined; now: number }) {
  const s = forecast.summary;
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div className="space-y-4">
        <Panel title="Source freshness">
          <Freshness forecast={forecast} turbine={turbine} now={now} />
        </Panel>
        <Panel title="The window at a glance">
          <dl data-testid="forecast-summary">
            <Fact label="Highest wind at hub">{value(s.max_wind_speed_hub_mps, "m/s")}</Fact>
            <Fact label="Highest gusts">{value(s.max_wind_gusts_10m_mps, "m/s")}</Fact>
            <Fact label="Rain in all">{value(s.total_precipitation_mm, "mm")}</Fact>
            <Fact label="Lowest visibility">{s.min_visibility_m === null ? "—" : `${(s.min_visibility_m / 1000).toFixed(1)} km`}</Fact>
            <Fact label="Thunderstorm energy">{value(s.max_cape_j_per_kg, "J/kg", 0)}</Fact>
            <Fact label="Hours covered">
              {s.hours_covered} of {s.hours_wanted}
            </Fact>
          </dl>
        </Panel>
        <Panel title="Provenance">
          <dl>
            <Fact label="Provider">{forecast.provider || "—"}</Fact>
            <Fact label="Model">{forecast.model || "—"}</Fact>
            <Fact label="Revision">{forecast.revision || "Not given"}</Fact>
            <Fact label="Retrieved">{when(forecast.retrieved_at)}</Fact>
            <Fact label="Licence">{forecast.licence || "—"}</Fact>
          </dl>
          <p className="mt-2 break-all text-xs text-slate-500">{forecast.forecast_id}</p>
        </Panel>
      </div>
      <div className="lg:col-span-2">
        <Panel title="Hour by hour">
          {forecast.points.length ? <Points forecast={forecast} /> : <p className="text-sm text-slate-500">No forecast hours to show.</p>}
        </Panel>
      </div>
    </div>
  );
}

export function WeatherScreen({ token }: { token: string }) {
  const api = useMemo(() => weatherApi(token), [token]);
  const fleet = useMemo(() => fleetApi(token), [token]);
  const [params, setParams] = useSearchParams();
  const [now] = useState(() => Date.now());
  const turbineId = params.get("turbine") ?? "";
  const hours = Number(params.get("hours") ?? 24) || 24;
  const loadFleet = useCallback(() => fleet.fleet(), [fleet]);
  const [farms] = useLoad(loadFleet);
  const loadForecast = useCallback(() => (turbineId ? api.forecast(turbineId, hours) : Promise.resolve(null)), [api, turbineId, hours]);
  const [forecast] = useLoad(loadForecast);
  const turbines = farms.kind === "ready" ? farms.data.wind_farms.flatMap((farm) => farm.turbines) : [];
  const turbine = turbines.find((t) => t.turbine_id === turbineId);
  const choose = (next: Record<string, string>) => setParams({ ...(turbineId ? { turbine: turbineId } : {}), hours: String(hours), ...next });
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Weather and sources</h2>
      <Panel title="Site">
        <Loading state={farms}>
          {(data) => (
            <div className="grid gap-3 sm:grid-cols-[2fr_auto] sm:items-end">
              <div>
                <label className={LABEL} htmlFor="weather-turbine">
                  Turbine
                </label>
                <select id="weather-turbine" className={INPUT} value={turbineId} onChange={(e) => choose({ turbine: e.target.value })}>
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
              <div role="group" aria-label="Window" className="flex flex-wrap gap-2">
                {WINDOWS.map((w) => (
                  <button
                    key={w.hours}
                    type="button"
                    aria-pressed={w.hours === hours}
                    onClick={() => choose({ hours: String(w.hours) })}
                    className={`rounded-full px-3 py-1 text-sm ring-1 ${w.hours === hours ? "bg-brand-primary text-white ring-brand-primary" : "bg-white text-slate-700 ring-slate-300"}`}
                  >
                    {w.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </Loading>
      </Panel>
      {turbineId ? (
        <Loading state={forecast}>{(found) => (found ? <ForecastPanels forecast={found} turbine={turbine} now={now} /> : null)}</Loading>
      ) : (
        <p className="text-sm text-slate-500">Choose a turbine to see its site's forecast.</p>
      )}
    </section>
  );
}
