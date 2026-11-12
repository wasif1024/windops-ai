import { useCallback, useState } from "react";
import { Loading, Panel } from "../../components/Panel";
import { useLoad } from "../../api/useLoad";
import type { FleetApi } from "./api";
import { METRICS } from "./format";
import { LineChart } from "./LineChart";
import { usePolling } from "./usePolling";

const WINDOWS = [5, 15, 60];
const DEFAULT_METRICS = ["power", "wind_speed", "gearbox_temperature", "vibration_rms"];
const REFRESH_MS = 10_000;

/** A turbine's telemetry charts: the metrics chosen, over the last minutes chosen. */
export function TelemetryCharts({ api, turbineId }: { api: FleetApi; turbineId: string }) {
  const [minutes, setMinutes] = useState(15);
  const [metrics, setMetrics] = useState<string[]>(DEFAULT_METRICS);
  const load = useCallback(() => api.telemetry(turbineId, minutes, metrics), [api, turbineId, minutes, metrics]);
  const [state, reload] = useLoad(load);
  usePolling(reload, REFRESH_MS);
  const toggle = (metric: string) =>
    setMetrics((current) =>
      current.includes(metric)
        ? current.length > 1
          ? current.filter((item) => item !== metric)
          : current
        : METRICS.map((item) => item.id).filter((id) => id === metric || current.includes(id)),
    );

  return (
    <Panel
      title="Telemetry"
      actions={
        <div role="group" aria-label="Time window" className="flex rounded-md border border-slate-300">
          {WINDOWS.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={minutes === value}
              onClick={() => setMinutes(value)}
              className={`px-3 py-1 text-sm first:rounded-l-md last:rounded-r-md ${
                minutes === value ? "bg-brand-primary text-white" : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              {value === 60 ? "1 h" : `${value} min`}
            </button>
          ))}
        </div>
      }
    >
      <fieldset>
        <legend className="sr-only">Metrics</legend>
        <div className="-mx-1 flex flex-wrap gap-1">
          {METRICS.map((metric) => (
            <label
              key={metric.id}
              className={`m-1 cursor-pointer rounded-full border px-3 py-1 text-xs ${
                metrics.includes(metric.id)
                  ? "border-brand-primary bg-brand-primary/10 text-slate-900"
                  : "border-slate-300 text-slate-600"
              }`}
            >
              <input
                type="checkbox"
                className="sr-only"
                checked={metrics.includes(metric.id)}
                onChange={() => toggle(metric.id)}
              />
              {metric.label}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="mt-4">
        <Loading state={state}>
          {(data) => (
            <div className="grid gap-4 md:grid-cols-2">
              {data.series.map((series) => (
                <LineChart key={series.metric} series={series} from={data.from_time} to={data.to_time} />
              ))}
            </div>
          )}
        </Loading>
      </div>
      <p className="mt-3 text-xs text-slate-500">
        One reading a second. Amber marks a suspect reading, one that came delayed or out of order. Readings are
        checked once each 10-minute period ends, so the newest are not yet checked. A gap is a reading the sensor did
        not send.
      </p>
    </Panel>
  );
}
