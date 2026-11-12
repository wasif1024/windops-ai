import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Loading, Panel, when } from "../../../components/Panel";
import { useLoad } from "../../../api/useLoad";
import type { FleetApi, TurbineDetail } from "../api";
import { FreshnessBadge, StatusBadge } from "../Freshness";
import { formatValue, labelOf, METRICS, reading, statusStyle, titleCase } from "../format";
import { TelemetryCharts } from "../TelemetryCharts";
import { usePolling } from "../usePolling";

const REFRESH_MS = 15_000;

function Facts({ turbine }: { turbine: TurbineDetail }) {
  const facts: [string, string][] = [
    ["Model", `${turbine.model.manufacturer} ${turbine.model.model}`.trim() || "—"],
    ["Rated power", formatValue(turbine.model.rated_power_kw, "kW")],
    ["Rotor diameter", formatValue(turbine.model.rotor_diameter_m, "m")],
    ["Hub height", formatValue(turbine.model.hub_height_m, "m")],
    ["Serial number", turbine.serial_number || "—"],
    ["Commissioned", turbine.commissioned_on || "—"],
    ["Location", `${turbine.location.latitude.toFixed(3)}, ${turbine.location.longitude.toFixed(3)}`],
    ["Status since", when(turbine.status_changed_at)],
  ];
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
      {facts.map(([term, value]) => (
        <div key={term} className="flex justify-between gap-3 border-b border-slate-100 py-1">
          <dt className="text-slate-500">{term}</dt>
          <dd className="text-right font-medium">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function Readings({ turbine }: { turbine: TurbineDetail }) {
  if (!turbine.latest) {
    return (
      <p className="text-sm text-slate-500">
        {turbine.telemetry_available ? "No readings in the last day." : "Telemetry cannot be read right now."}
      </p>
    );
  }
  return (
    <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4" aria-label="Latest readings">
      {METRICS.map((metric) => {
        const value = reading(turbine.latest, metric.id);
        return (
          <li key={metric.id} className="rounded-md border border-slate-200 p-2">
            <span className="block text-xs text-slate-500">{labelOf(metric.id)}</span>
            <span className={`block font-medium ${value?.quality === "suspect" ? "text-amber-800" : ""}`}>
              {formatValue(value?.value ?? null, value?.unit ?? "")}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** One turbine: its facts, components, newest readings and telemetry charts (SRS 4.11). */
export function TurbinePage({ api, turbineId, back }: { api: FleetApi; turbineId: string; back: string }) {
  const load = useCallback(() => api.turbine(turbineId), [api, turbineId]);
  const [state, reload] = useLoad(load);
  const [now, setNow] = useState(() => Date.now());
  const refresh = useCallback(() => {
    reload();
    setNow(Date.now());
  }, [reload]);
  usePolling(refresh, REFRESH_MS);

  return (
    <div className="space-y-4">
      <Link to={back} className="text-sm text-brand-primary hover:underline">
        ← Fleet overview
      </Link>
      <Loading state={state}>
        {(turbine) => (
          <>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h2 className="text-xl font-semibold">{turbine.name}</h2>
              <StatusBadge status={titleCase(turbine.status)} className={statusStyle(turbine.status)} />
              <FreshnessBadge latest={turbine.latest} now={now} />
            </div>
            <p className="text-sm text-slate-600">
              {turbine.turbine_id} · {turbine.wind_farm_id}
              {turbine.status_reason && ` · ${turbine.status_reason}`}
            </p>
            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title="Turbine">
                <Facts turbine={turbine} />
              </Panel>
              <Panel title="Latest readings">
                <Readings turbine={turbine} />
                {turbine.latest && (
                  <p className="mt-2 text-xs text-slate-500">
                    {titleCase(turbine.latest.operating_state)}, measured {when(turbine.latest.event_time)}, stored{" "}
                    {turbine.latest.delay_seconds.toFixed(1)} s later.
                  </p>
                )}
              </Panel>
            </div>
            <TelemetryCharts api={api} turbineId={turbine.turbine_id} />
            <Panel title="Components">
              {turbine.components.length === 0 ? (
                <p className="text-sm text-slate-500">No components are recorded.</p>
              ) : (
                <ul className="divide-y divide-slate-100 text-sm" aria-label="Components">
                  {turbine.components.map((component) => (
                    <li key={component.component_id} className="grid gap-1 py-2 sm:grid-cols-[1fr_auto] sm:items-center">
                      <span className="min-w-0">
                        <span className="block font-medium">{component.name}</span>
                        <span className="block text-xs text-slate-500">
                          {[titleCase(component.kind), component.manufacturer, component.model].filter(Boolean).join(" · ")}
                        </span>
                      </span>
                      <StatusBadge
                        status={titleCase(component.status)}
                        className={
                          component.status === "in_service"
                            ? "bg-emerald-100 text-emerald-900"
                            : component.status === "degraded"
                              ? "bg-amber-100 text-amber-900"
                              : "bg-red-100 text-red-900"
                        }
                      />
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </>
        )}
      </Loading>
    </div>
  );
}
