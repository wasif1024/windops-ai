import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { usePolling } from "../../fleet/usePolling";
import { type Alarm, type AlarmsApi, SEVERITY_STYLE, STATE_STYLE, type Transition, severitySource } from "../api";

const REFRESH_MS = 10_000;
const DEFAULT_SHELVE_MINUTES = 30;
const SHELVE_FOR = [
  { minutes: 30, label: "30 minutes" },
  { minutes: 120, label: "2 hours" },
  { minutes: 480, label: "8 hours" },
  { minutes: 1440, label: "24 hours" },
];

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-slate-100 py-1.5 text-sm">
      <dt className="text-slate-500">{label}</dt>
      <dd className="break-words text-right font-medium text-slate-800">{value || "—"}</dd>
    </div>
  );
}

function who(transition: Transition): string {
  if (transition.actor_type === "device") return "The turbine's controller";
  if (transition.actor_type === "system") return "WindOps";
  return transition.actor_id;
}

function History({ transitions }: { transitions: Transition[] }) {
  return (
    <ol className="space-y-3" aria-label="History">
      {transitions.map((t, index) => (
        <li key={`${t.at}-${index}`} className="border-l-2 border-slate-200 pl-3 text-sm">
          <p className="font-medium text-slate-800">
            {t.from_state ? `${titleCase(t.from_state)} → ${titleCase(t.to_state)}` : `Raised as ${t.to_state}`}
          </p>
          <p className="text-xs text-slate-500">
            {when(t.at)} · {who(t)}
          </p>
          {t.note && <p className="mt-1 text-slate-700">“{t.note}”</p>}
        </li>
      ))}
    </ol>
  );
}

/** What an operator may do to this alarm, as its state allows. */
function Actions({ api, alarm, onDone }: { api: AlarmsApi; alarm: Alarm; onDone: () => void }) {
  const change = useChange();
  const [comment, setComment] = useState("");
  const [reason, setReason] = useState("");
  const [minutes, setMinutes] = useState(DEFAULT_SHELVE_MINUTES);
  const run = async (action: (key: string) => Promise<Alarm>) => {
    if (await change.run(action)) onDone();
  };
  const busy = change.state.kind === "busy";
  if (alarm.state === "cleared") {
    return <p className="text-sm text-slate-500">Cleared by the turbine. Nothing more to do.</p>;
  }
  return (
    <div className="space-y-5">
      {alarm.state === "active" && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            void run((key) => api.acknowledge(alarm.alarm_id, key, comment.trim()));
          }}
        >
          <label className={LABEL} htmlFor="ack-comment">
            Comment, if any
          </label>
          <input id="ack-comment" className={INPUT} value={comment} maxLength={500} onChange={(e) => setComment(e.target.value)} />
          <Button type="submit" disabled={busy}>
            Acknowledge
          </Button>
        </form>
      )}
      {(alarm.state === "active" || alarm.state === "acknowledged") && (
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            void run((key) => api.shelve(alarm.alarm_id, key, reason.trim(), minutes));
          }}
        >
          <label className={LABEL} htmlFor="shelve-reason">
            Reason to shelve
          </label>
          <input id="shelve-reason" className={INPUT} value={reason} maxLength={500} required onChange={(e) => setReason(e.target.value)} />
          <label className={LABEL} htmlFor="shelve-for">
            Shelve for
          </label>
          <select id="shelve-for" className={INPUT} value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
            {SHELVE_FOR.map((option) => (
              <option key={option.minutes} value={option.minutes}>
                {option.label}
              </option>
            ))}
          </select>
          <Button type="submit" variant="outline" disabled={busy || !reason.trim()}>
            Shelve
          </Button>
        </form>
      )}
      {alarm.state === "shelved" && (
        <Button variant="outline" disabled={busy} onClick={() => void run((key) => api.unshelve(alarm.alarm_id, key))}>
          Unshelve now
        </Button>
      )}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </div>
  );
}

/** One alarm: what it is, where its severity came from, every change, and what to do now. */
export function AlarmPage({ api, alarmId, canAct }: { api: AlarmsApi; alarmId: string; canAct: boolean }) {
  const load = useCallback(() => api.get(alarmId), [api, alarmId]);
  const [state, reload] = useLoad(load);
  usePolling(reload, REFRESH_MS);
  return (
    <section className="space-y-4">
      <Link to="?" className="text-sm text-brand-primary hover:underline">
        ← Alarm console
      </Link>
      <Loading state={state}>
        {({ alarm, transitions }) => (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-xl font-semibold">{alarm.description}</h2>
              <StatusBadge status={titleCase(alarm.severity)} className={SEVERITY_STYLE[alarm.severity]} />
              <StatusBadge status={titleCase(alarm.state)} className={STATE_STYLE[alarm.state]} />
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title="Alarm">
                <dl>
                  <Fact label="Turbine" value={alarm.turbine_id} />
                  <Fact label="Code" value={alarm.code} />
                  <Fact label="Tag" value={alarm.tag} />
                  <Fact label="Raised" value={when(alarm.raised_at)} />
                  <Fact label="Severity" value={`${titleCase(alarm.severity)}: ${severitySource(alarm.severity_source)}`} />
                  <Fact label="Repeated while standing" value={String(alarm.repeat_count)} />
                  <Fact label="Acknowledged" value={alarm.acknowledged_by ? `${when(alarm.acknowledged_at)} by ${alarm.acknowledged_by}` : ""} />
                  <Fact label="Shelved until" value={alarm.shelved_until ? `${when(alarm.shelved_until)}: ${alarm.shelve_reason}` : ""} />
                  <Fact label="Cleared" value={when(alarm.cleared_at)} />
                </dl>
              </Panel>
              <Panel title="What to do">
                {canAct ? (
                  <Actions api={api} alarm={alarm} onDone={reload} />
                ) : (
                  <p className="text-sm text-slate-500">Your roles let you read alarms, not acknowledge or shelve them.</p>
                )}
              </Panel>
            </div>
            <Panel title="History">
              <History transitions={transitions} />
            </Panel>
          </>
        )}
      </Loading>
    </section>
  );
}
