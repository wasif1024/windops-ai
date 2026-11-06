import { type FormEvent, useCallback, useState } from "react";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { BREAK_GLASS_MINUTES, BREAK_GLASS_REASON_MINIMUM, type PlatformApi } from "../api";

const PLATFORM_TENANT = "windops-platform";

function Start({ api, tenants, onStarted }: { api: PlatformApi; tenants: string[]; onStarted: () => void }) {
  const [tenant, setTenant] = useState(tenants[0] ?? "");
  const [reason, setReason] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [problem, setProblem] = useState<string | null>(null);
  const change = useChange();
  const start = async (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim().length < BREAK_GLASS_REASON_MINIMUM) {
      setProblem(`Give a reason of at least ${BREAK_GLASS_REASON_MINIMUM} characters, such as the incident number.`);
      return;
    }
    if (!(minutes >= BREAK_GLASS_MINUTES.minimum && minutes <= BREAK_GLASS_MINUTES.maximum)) {
      setProblem(`Choose from ${BREAK_GLASS_MINUTES.minimum} to ${BREAK_GLASS_MINUTES.maximum} minutes.`);
      return;
    }
    setProblem(null);
    if (await change.run((key) => api.startBreakGlass(key, tenant, reason.trim(), minutes))) {
      setReason("");
      onStarted();
    }
  };
  return (
    <Panel title="Start break-glass access">
      <p className="text-sm text-slate-600">
        Read-only access to one tenant's data, for a limited time. The reason goes into that tenant's audit trail, so
        the tenant can see it (RBAC-003).
      </p>
      <form onSubmit={(event) => void start(event)} className="mt-3 grid gap-3 md:grid-cols-[1fr_8rem]">
        <label className={LABEL}>
          Tenant
          <select className={`${INPUT} mt-1`} value={tenant} onChange={(event) => setTenant(event.target.value)}>
            {tenants.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>
        <label className={LABEL}>
          Minutes
          <input
            type="number"
            className={`${INPUT} mt-1`}
            min={BREAK_GLASS_MINUTES.minimum}
            max={BREAK_GLASS_MINUTES.maximum}
            value={minutes}
            onChange={(event) => setMinutes(Number(event.target.value))}
          />
        </label>
        <label className={`${LABEL} md:col-span-2`}>
          Reason
          <textarea
            className={`${INPUT} mt-1`}
            rows={2}
            maxLength={500}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <div className="md:col-span-2">
          {problem && <p className="mb-2 text-sm text-red-800">{problem}</p>}
          <Button type="submit" disabled={change.state.kind === "busy" || !tenant}>
            Start access
          </Button>
          <ChangeStatus state={change.state} onDismiss={change.reset} />
        </div>
      </form>
    </Panel>
  );
}

function Active({ api }: { api: PlatformApi }) {
  const [state, reload] = useLoad(useCallback(() => api.breakGlass(), [api]));
  const change = useChange();
  const end = async (grantId: string) => {
    if (await change.run((key) => api.endBreakGlass(key, grantId))) reload();
  };
  return (
    <Panel title="Your active access">
      <Loading state={state}>
        {({ grants }) =>
          grants.length === 0 ? (
            <p className="text-sm text-slate-500">None. Access ends by itself when its time is up.</p>
          ) : (
            <ul className="divide-y divide-slate-100 text-sm" aria-label="Active break-glass access">
              {grants.map((grant) => (
                <li key={grant.grant_id} className="flex flex-wrap items-start justify-between gap-2 py-3">
                  <span className="grid gap-1">
                    <span className="font-mono font-medium">{grant.tenant_id}</span>
                    <span className="text-slate-600">{grant.reason}</span>
                    <span className="text-xs text-slate-500">Until {when(grant.expires_at)}</span>
                  </span>
                  <Button
                    variant="outline"
                    className="text-red-700"
                    aria-label={`End access to ${grant.tenant_id}`}
                    onClick={() => void end(grant.grant_id)}
                  >
                    End now
                  </Button>
                </li>
              ))}
            </ul>
          )
        }
      </Loading>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </Panel>
  );
}

export function BreakGlassPage({ api }: { api: PlatformApi }) {
  const [tenants] = useLoad(useCallback(() => api.tenants(), [api]));
  const [version, setVersion] = useState(0);
  return (
    <div className="grid gap-4">
      <Loading state={tenants}>
        {({ tenants: list }) => (
          <Start
            api={api}
            // Platform staff's own tenant holds no customer data, so it is not offered.
            tenants={list
              .filter((item) => item.status === "active" && item.tenant_id !== PLATFORM_TENANT)
              .map((item) => item.tenant_id)}
            onStarted={() => setVersion((value) => value + 1)}
          />
        )}
      </Loading>
      {/* A new key after each start, so the list is read again. */}
      <Active key={version} api={api} />
    </div>
  );
}
