// Each path out of the platform, which a tenant can turn off (EGR-002, ADR 0048). The Tenant
// Service decides: it needs mcp.configure, a person, a recent second factor and a reason.
import { useCallback, useState } from "react";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import type { AdminApi, EgressPath } from "../api";

export const TITLES: Record<string, string> = {
  claude: "Claude",
  embeddings: "Embeddings",
  langsmith_traces: "LangSmith traces",
  langsmith_sandbox: "LangSmith Sandbox",
  forecasts: "Weather forecasts",
};
export const REASON_MINIMUM = 10;

function Path({ api, path, onSaved }: { api: AdminApi; path: EgressPath; onSaved: (p: EgressPath) => void }) {
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const change = useChange();
  const title = TITLES[path.path] ?? path.path;
  const turnTo = !path.enabled;

  const save = async () => {
    const result = await change.run((key) => api.setEgress(key, path.path, turnTo, reason.trim()));
    if (result) {
      setAsking(false);
      setReason("");
      onSaved(result);
    }
  };

  return (
    <li className="border-t border-slate-100 py-3 first:border-t-0" data-testid={`egress-${path.path}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 className="font-semibold text-slate-800">{title}</h4>
          <p className="text-slate-600">{path.destination}</p>
        </div>
        <span
          className={`shrink-0 rounded px-2 py-0.5 text-xs font-medium ${
            path.enabled ? "bg-emerald-50 text-emerald-800" : "bg-amber-100 text-amber-900"
          }`}
        >
          {path.enabled ? "On" : "Off"}
        </span>
      </div>
      <p className={`mt-1 ${path.enabled ? "text-slate-500" : "text-amber-900"}`}>
        {path.enabled ? "While off: " : "Now: "}
        {path.when_off}
      </p>
      {path.updated_at && (
        <p className="mt-1 text-xs text-slate-500">
          Turned {path.enabled ? "on" : "off"} {when(path.updated_at)} by{" "}
          <span className="break-all font-mono">{path.updated_by}</span>: {path.reason}
        </p>
      )}
      {asking ? (
        <div className="mt-2 space-y-2">
          <label className={LABEL}>
            Why turn {title} {turnTo ? "on" : "off"}?
            <input
              className={`${INPUT} mt-1`}
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => void save()}
              disabled={reason.trim().length < REASON_MINIMUM || change.state.kind === "busy"}
            >
              Turn {turnTo ? "on" : "off"}
            </Button>
            <Button variant="outline" onClick={() => setAsking(false)}>
              Cancel
            </Button>
          </div>
          {reason.trim().length < REASON_MINIMUM && (
            <p className="text-xs text-slate-500">A reason of at least {REASON_MINIMUM} characters is kept with it.</p>
          )}
        </div>
      ) : (
        <Button variant="outline" className="mt-2" onClick={() => setAsking(true)}>
          Turn {turnTo ? "on" : "off"}
        </Button>
      )}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </li>
  );
}

/** The tenant's egress switches, with what each sends and what stops while it is off. */
export function EgressPage({ api }: { api: AdminApi }) {
  const [state, reload] = useLoad(useCallback(async () => (await api.egress()).paths, [api]));
  return (
    <Panel title="Data leaving the platform">
      <div className="space-y-3 text-sm">
        <p className="text-slate-600">
          Each of these sends some of your data to a provider outside the platform. Turn one off and nothing more goes
          there for your tenant. What stops is shown, and the screens that depend on it say why.
        </p>
        <Loading state={state}>
          {(paths) => (
            <ul aria-label="Paths out of the platform">
              {paths.map((path) => (
                <Path key={path.path} api={api} path={path} onSaved={() => reload()} />
              ))}
            </ul>
          )}
        </Loading>
        <p className="text-slate-500">
          Web research has its own switch, in its research policy, which those who manage knowledge set.
        </p>
      </div>
    </Panel>
  );
}
