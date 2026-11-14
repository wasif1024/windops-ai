import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { Loading, Panel } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { usePolling } from "../../fleet/usePolling";
import { type Alarm, type AlarmState, type AlarmsApi, ago, SEVERITY_STYLE, STANDING, STATE_STYLE } from "../api";

const REFRESH_MS = 10_000;
interface View {
  id: string;
  label: string;
  states: AlarmState[];
}
const STANDING_VIEW: View = { id: "standing", label: "Standing", states: STANDING };
const VIEWS: View[] = [
  STANDING_VIEW,
  { id: "active", label: "Active", states: ["active"] },
  { id: "acknowledged", label: "Acknowledged", states: ["acknowledged"] },
  { id: "shelved", label: "Shelved", states: ["shelved"] },
  { id: "cleared", label: "Cleared", states: ["cleared"] },
];

function AlarmRow({ alarm, now }: { alarm: Alarm; now: number }) {
  return (
    <li>
      <Link
        to={`?alarm=${encodeURIComponent(alarm.alarm_id)}`}
        className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-3 hover:border-brand-primary hover:shadow-sm sm:flex-row sm:items-center"
        data-testid={`alarm-${alarm.alarm_id}`}
      >
        <span className="flex shrink-0 items-center gap-2 sm:w-44">
          <StatusBadge status={titleCase(alarm.severity)} className={SEVERITY_STYLE[alarm.severity]} />
          <StatusBadge status={titleCase(alarm.state)} className={STATE_STYLE[alarm.state]} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block break-words font-medium text-slate-900">{alarm.description}</span>
          <span className="block break-all text-xs text-slate-500">
            {alarm.code}
            {alarm.tag && ` · ${alarm.tag}`}
            {alarm.repeat_count > 0 && ` · repeated ${alarm.repeat_count}×`}
          </span>
        </span>
        <span className="flex shrink-0 items-baseline justify-between gap-3 text-sm sm:w-56 sm:justify-end">
          <span className="font-medium text-slate-700">{alarm.turbine_id}</span>
          <span className="text-xs text-slate-500">{ago(alarm.raised_at, now)}</span>
        </span>
      </Link>
    </li>
  );
}

/** Every alarm the user may see, most severe and newest first, kept current (SRS 4.11). */
export function AlarmConsole({ api }: { api: AlarmsApi }) {
  const [view, setView] = useState(STANDING_VIEW);
  const load = useCallback(() => api.list(view.states), [api, view]);
  const [state, reload] = useLoad(load);
  const [now, setNow] = useState(() => Date.now());
  const refresh = useCallback(() => {
    reload();
    setNow(Date.now());
  }, [reload]);
  usePolling(refresh, REFRESH_MS);
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xl font-semibold">Alarm console</h2>
        <Button variant="outline" onClick={refresh}>
          Refresh
        </Button>
      </div>
      <Panel title={`${view.label} alarms`}>
        <div role="group" aria-label="Show" className="mb-4 flex flex-wrap gap-2">
          {VIEWS.map((option) => (
            <button
              key={option.id}
              type="button"
              aria-pressed={option.id === view.id}
              onClick={() => setView(option)}
              className={`rounded-full px-3 py-1 text-sm ring-1 ${
                option.id === view.id ? "bg-brand-primary text-white ring-brand-primary" : "bg-white text-slate-700 ring-slate-300"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
        <Loading state={state}>
          {(data) =>
            data.alarms.length === 0 ? (
              <p className="text-slate-500" data-testid="no-alarms">
                No {view.id === "standing" ? "standing" : view.label.toLowerCase()} alarms.
              </p>
            ) : (
              <ul className="space-y-2" aria-label="Alarms">
                {data.alarms.map((alarm) => (
                  <AlarmRow key={alarm.alarm_id} alarm={alarm} now={now} />
                ))}
              </ul>
            )
          }
        </Loading>
      </Panel>
    </section>
  );
}
