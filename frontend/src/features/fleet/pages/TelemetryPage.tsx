import { useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { Loading, Panel } from "../../../components/Panel";
import { useLoad } from "../../../api/useLoad";
import type { FleetApi } from "../api";
import { TelemetryCharts } from "../TelemetryCharts";

/** Charts for one turbine at a time, chosen from the fleet the user may read (SRS 4.11). */
export function TelemetryPage({ api }: { api: FleetApi }) {
  const load = useCallback(() => api.fleet(), [api]);
  const [state] = useLoad(load);
  const [params, setParams] = useSearchParams();
  const chosen = params.get("turbine");

  return (
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">Telemetry</h2>
      <Loading state={state}>
        {(fleet) => {
          const turbines = fleet.wind_farms.flatMap((farm) =>
            farm.turbines.map((turbine) => ({ ...turbine, farm: farm.name })),
          );
          const selected = turbines.find((turbine) => turbine.turbine_id === chosen) ?? turbines[0];
          if (!selected) return <p className="text-sm text-slate-500">No turbines to show.</p>;
          return (
            <>
              <Panel title="Turbine">
                <label className="block text-sm font-medium text-slate-700" htmlFor="telemetry-turbine">
                  Show telemetry for
                </label>
                <select
                  id="telemetry-turbine"
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm sm:w-auto"
                  value={selected.turbine_id}
                  onChange={(event) => setParams({ turbine: event.target.value })}
                >
                  {fleet.wind_farms.map((farm) => (
                    <optgroup key={farm.wind_farm_id} label={farm.name}>
                      {farm.turbines.map((turbine) => (
                        <option key={turbine.turbine_id} value={turbine.turbine_id}>
                          {turbine.name} ({turbine.turbine_id})
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </Panel>
              <TelemetryCharts key={selected.turbine_id} api={api} turbineId={selected.turbine_id} />
            </>
          );
        }}
      </Loading>
    </div>
  );
}
