import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "../../../components/Button";
import { Loading, Panel } from "../../../components/Panel";
import { useLoad } from "../../../api/useLoad";
import type { FleetApi, TurbineSummary } from "../api";
import { FreshnessBadge, StatusBadge } from "../Freshness";
import { formatValue, reading, statusStyle, titleCase } from "../format";
import { usePolling } from "../usePolling";

const REFRESH_MS = 15_000;

function TurbineCard({ turbine, now }: { turbine: TurbineSummary; now: number }) {
  const power = reading(turbine.latest, "power");
  const wind = reading(turbine.latest, "wind_speed");
  const share =
    power?.value != null && turbine.rated_power_kw > 0 ? Math.round((power.value / turbine.rated_power_kw) * 100) : null;
  return (
    <li>
      <Link
        to={`?turbine=${encodeURIComponent(turbine.turbine_id)}`}
        className="block h-full rounded-lg border border-slate-200 bg-white p-3 hover:border-brand-primary hover:shadow-sm"
        data-testid={`turbine-${turbine.turbine_id}`}
      >
        <div className="flex items-start justify-between gap-2">
          <span className="min-w-0">
            <span className="block break-words font-medium text-slate-900">{turbine.name}</span>
            <span className="block truncate text-xs text-slate-500">{turbine.turbine_id}</span>
          </span>
          <StatusBadge status={titleCase(turbine.status)} className={statusStyle(turbine.status)} />
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-2 text-sm">
          <div>
            <dt className="text-xs text-slate-500">Power</dt>
            <dd className="font-medium">
              {formatValue(power?.value ?? null, power?.unit ?? "kW")}
              {share !== null && <span className="ml-1 text-xs font-normal text-slate-500">{share}%</span>}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Wind</dt>
            <dd className="font-medium">{formatValue(wind?.value ?? null, wind?.unit ?? "m/s")}</dd>
          </div>
        </dl>
        <div className="mt-2">
          <FreshnessBadge latest={turbine.latest} now={now} />
        </div>
      </Link>
    </li>
  );
}

/** Every wind farm the user may see, and each turbine's status and newest readings (SRS 4.11). */
export function FleetPage({ api }: { api: FleetApi }) {
  const load = useCallback(() => api.fleet(), [api]);
  const [state, reload] = useLoad(load);
  const [now, setNow] = useState(() => Date.now());
  const refresh = useCallback(() => {
    reload();
    setNow(Date.now());
  }, [reload]);
  usePolling(refresh, REFRESH_MS);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xl font-semibold">Fleet overview</h2>
        <Button variant="outline" className="text-brand-primary" onClick={refresh}>
          Refresh
        </Button>
      </div>
      <Loading state={state}>
        {(fleet) => (
          <>
            {!fleet.telemetry_available && (
              <p role="status" className="rounded-md bg-amber-50 p-3 text-sm text-amber-900">
                Telemetry cannot be read right now, so no readings are shown. The turbines' statuses are current.
              </p>
            )}
            {fleet.wind_farms.length === 0 ? (
              <p className="text-sm text-slate-500">No wind farms to show. Your roles may not include any.</p>
            ) : (
              fleet.wind_farms.map((farm) => {
                const running = farm.turbines.filter((t) => t.status === "operating").length;
                const output = farm.turbines.reduce((sum, t) => sum + (reading(t.latest, "power")?.value ?? 0), 0);
                return (
                  <Panel key={farm.wind_farm_id} title={farm.name}>
                    <p className="text-sm text-slate-600">
                      {farm.offshore ? "Offshore" : "Onshore"} · {farm.country} · {running} of {farm.turbines.length}{" "}
                      operating · {formatValue(output, "kW")} now
                    </p>
                    <ul
                      className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
                      aria-label={`${farm.name} turbines`}
                    >
                      {farm.turbines.map((turbine) => (
                        <TurbineCard key={turbine.turbine_id} turbine={turbine} now={now} />
                      ))}
                    </ul>
                  </Panel>
                );
              })
            )}
          </>
        )}
      </Loading>
    </div>
  );
}
