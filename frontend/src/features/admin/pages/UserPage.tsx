import { type FormEvent, useCallback, useId, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "../../../components/Button";
import type { AdminApi, RoleGrant, User } from "../api";
import { FIRST_ROLE, roleTitle, TENANT_ROLES } from "../catalog";
import { ChangeStatus } from "../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../components/Panel";
import { useChange } from "../useChange";
import { useLoad } from "../useLoad";

function where(grant: RoleGrant): string {
  if (grant.turbine_id) return `Turbine ${grant.turbine_id} in ${grant.wind_farm_id}`;
  if (grant.wind_farm_id) return `Wind farm ${grant.wind_farm_id}`;
  return "Whole tenant";
}

function Status({ api, user, onChanged }: { api: AdminApi; user: User; onChanged: (user: User) => void }) {
  const change = useChange();
  const toggle = async () => {
    const updated = await change.run((key) =>
      user.enabled ? api.disable(key, user.member_id) : api.reactivate(key, user.member_id),
    );
    if (updated) onChanged(updated);
  };
  return (
    <Panel
      title={user.email}
      actions={
        <Button
          variant={user.enabled ? "outline" : "primary"}
          className={user.enabled ? "text-red-700" : ""}
          disabled={change.state.kind === "busy"}
          onClick={() => void toggle()}
        >
          {user.enabled ? "Disable user" : "Reactivate user"}
        </Button>
      }
    >
      <dl className="grid gap-2 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="text-slate-500">Name</dt>
        <dd>{[user.first_name, user.last_name].filter(Boolean).join(" ") || "—"}</dd>
        <dt className="text-slate-500">Status</dt>
        <dd data-testid="user-status">{user.enabled ? "Active" : "Disabled"}</dd>
        <dt className="text-slate-500">Added</dt>
        <dd>{when(user.created_at)}</dd>
      </dl>
      {user.enabled && <p className="mt-2 text-xs text-slate-500">Disabling also ends every session they have.</p>}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </Panel>
  );
}

function Mfa({ api, id }: { api: AdminApi; id: string }) {
  const [state] = useLoad(useCallback(() => api.mfa(id), [api, id]));
  return (
    <Panel title="Second factor">
      <Loading state={state}>
        {(mfa) => (
          <ul className="grid gap-1 text-sm" data-testid="mfa-status">
            <li>Authenticator app: {mfa.otp_configured ? "set up" : "not set up"}</li>
            <li>Recovery codes: {mfa.recovery_codes_configured ? "set up" : "not set up"}</li>
            {mfa.setup_required && <li className="text-amber-800">They will be asked to set one up at next sign-in.</li>}
          </ul>
        )}
      </Loading>
    </Panel>
  );
}

function Sessions({ api, id }: { api: AdminApi; id: string }) {
  const [state, reload] = useLoad(useCallback(() => api.sessions(id), [api, id]));
  const change = useChange();
  const end = async () => {
    if (await change.run((key) => api.revokeSessions(key, id))) reload();
  };
  return (
    <Panel
      title="Sessions"
      actions={
        <Button variant="outline" className="text-brand-primary" onClick={() => void end()}>
          End all sessions
        </Button>
      }
    >
      <Loading state={state}>
        {({ sessions }) =>
          sessions.length === 0 ? (
            <p className="text-sm text-slate-500">No sessions.</p>
          ) : (
            <ul className="divide-y divide-slate-100 text-sm" aria-label="Sessions">
              {sessions.map((session) => (
                <li key={session.session_id} className="grid gap-1 py-2 md:grid-cols-[8rem_1fr_1fr]">
                  <span className="font-mono">{session.ip_address}</span>
                  <span>Started {when(session.started_at)}</span>
                  <span className="text-slate-500">Last used {when(session.last_access_at)}</span>
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

function Roles({ api, id }: { api: AdminApi; id: string }) {
  const [state, reload] = useLoad(useCallback(() => api.grants(id), [api, id]));
  const [role, setRole] = useState<string>(FIRST_ROLE);
  const [farm, setFarm] = useState("");
  const [turbine, setTurbine] = useState("");
  const [scopeError, setScopeError] = useState(false);
  const change = useChange();
  // Named by its label alone, not by the chosen option as well.
  const field = useId();

  const grant = async (event: FormEvent) => {
    event.preventDefault();
    // A turbine is always named within its wind farm (RBAC-002).
    if (turbine.trim() && !farm.trim()) {
      setScopeError(true);
      return;
    }
    setScopeError(false);
    const body = { user_id: id, role, wind_farm_id: farm.trim(), turbine_id: turbine.trim() };
    if (await change.run((key) => api.grant(key, body))) {
      setFarm("");
      setTurbine("");
      reload();
    }
  };
  const revoke = async (grantId: string) => {
    if (await change.run((key) => api.revoke(key, grantId))) reload();
  };

  return (
    <Panel title="Roles">
      <Loading state={state}>
        {({ grants }) => {
          const active = grants.filter((item) => !item.revoked_at);
          const ended = grants.filter((item) => item.revoked_at);
          return (
            <>
              {active.length === 0 ? (
                <p className="text-sm text-slate-500">No roles yet.</p>
              ) : (
                <ul className="divide-y divide-slate-100 text-sm" aria-label="Roles">
                  {active.map((item) => (
                    <li key={item.grant_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <span>
                        <span className="font-medium">{roleTitle(item.role)}</span>
                        <span className="ml-2 text-slate-500">{where(item)}</span>
                      </span>
                      <Button
                        variant="outline"
                        className="text-red-700"
                        aria-label={`Revoke ${roleTitle(item.role)}`}
                        onClick={() => void revoke(item.grant_id)}
                      >
                        Revoke
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
              {ended.length > 0 && (
                <details className="mt-2 text-sm">
                  <summary className="cursor-pointer text-slate-500">Ended roles ({ended.length})</summary>
                  <ul className="mt-1 grid gap-1 text-slate-500">
                    {ended.map((item) => (
                      <li key={item.grant_id}>
                        {roleTitle(item.role)}, {where(item)}, ended {when(item.revoked_at)}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </>
          );
        }}
      </Loading>
      <form onSubmit={(event) => void grant(event)} className="mt-4 grid gap-3 md:grid-cols-[2fr_1fr_1fr_auto] md:items-end">
        <div>
          <label htmlFor={field} className={LABEL}>
            Role
          </label>
          <select
            id={field}
            className={`${INPUT} mt-1`}
            value={role}
            onChange={(event) => setRole(event.target.value)}
          >
            {TENANT_ROLES.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
        </div>
        <label className={LABEL}>
          Wind farm (optional)
          <input className={`${INPUT} mt-1`} maxLength={64} value={farm} onChange={(e) => setFarm(e.target.value)} />
        </label>
        <label className={LABEL}>
          Turbine (optional)
          <input
            className={`${INPUT} mt-1`}
            maxLength={64}
            value={turbine}
            onChange={(e) => setTurbine(e.target.value)}
          />
        </label>
        <Button type="submit" disabled={change.state.kind === "busy"}>
          Give role
        </Button>
        {scopeError && <p className="text-sm text-red-800 md:col-span-4">Name the turbine's wind farm too.</p>}
      </form>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </Panel>
  );
}

export function UserPage({ api, id, canGrant }: { api: AdminApi; id: string; canGrant: boolean }) {
  const [state] = useLoad(useCallback(() => api.user(id), [api, id]));
  const [updated, setUpdated] = useState<User | null>(null);
  return (
    <div className="grid gap-4">
      <Link to="?tab=users" className="text-sm font-medium text-brand-primary">
        ← All users
      </Link>
      <Loading state={state}>
        {(loaded) => {
          const user = updated?.member_id === loaded.member_id ? updated : loaded;
          return (
            <>
              <Status api={api} user={user} onChanged={setUpdated} />
              <div className="grid gap-4 lg:grid-cols-2">
                <Mfa api={api} id={id} />
                <Sessions api={api} id={id} />
              </div>
              {canGrant && <Roles api={api} id={id} />}
            </>
          );
        }}
      </Loading>
    </div>
  );
}
