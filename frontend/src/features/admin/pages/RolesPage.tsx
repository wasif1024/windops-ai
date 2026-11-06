import { useCallback, useId, useState } from "react";
import { Button } from "../../../components/Button";
import type { AdminApi, RolePermissions } from "../api";
import { FIRST_ROLE, TENANT_PERMISSIONS, TENANT_ROLES } from "../catalog";
import { ChangeStatus } from "../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel } from "../components/Panel";
import { useChange } from "../useChange";
import { useLoad } from "../useLoad";

function Editor({ api, mapping }: { api: AdminApi; mapping: RolePermissions }) {
  const [chosen, setChosen] = useState(() => new Set(mapping.permissions));
  const [saved, setSaved] = useState(mapping);
  const change = useChange();
  const changed =
    chosen.size !== saved.permissions.length || saved.permissions.some((permission) => !chosen.has(permission));

  const toggle = (permission: string) =>
    setChosen((current) => {
      const next = new Set(current);
      if (next.has(permission)) next.delete(permission);
      else next.add(permission);
      return next;
    });
  const save = async () => {
    const result = await change.run((key) => api.setRolePermissions(key, mapping.role, [...chosen].sort()));
    if (result) {
      setSaved(result);
      setChosen(new Set(result.permissions));
    }
  };

  return (
    <>
      <p className="text-sm text-slate-500">
        {saved.customized ? "Changed for this tenant." : "The standard permissions (SRS 3.2)."} Each takes effect at once.
      </p>
      <fieldset className="mt-3">
        <legend className="sr-only">Permissions</legend>
        <ul className="grid gap-x-4 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
          {TENANT_PERMISSIONS.map((permission) => (
            <li key={permission}>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4"
                  checked={chosen.has(permission)}
                  onChange={() => toggle(permission)}
                />
                <span className="font-mono">{permission}</span>
              </label>
            </li>
          ))}
        </ul>
      </fieldset>
      <div className="mt-4 flex flex-wrap gap-2">
        <Button disabled={!changed || change.state.kind === "busy"} onClick={() => void save()}>
          Save permissions
        </Button>
        <Button
          variant="outline"
          className="text-brand-primary"
          disabled={!changed}
          onClick={() => setChosen(new Set(saved.permissions))}
        >
          Undo changes
        </Button>
      </div>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </>
  );
}

export function RolesPage({ api }: { api: AdminApi }) {
  const [role, setRole] = useState<string>(FIRST_ROLE);
  const [state] = useLoad(useCallback(() => api.rolePermissions(role), [api, role]));
  // Named by its label alone, not by the chosen option as well.
  const field = useId();
  return (
    <Panel title="Role permissions">
      <div className="max-w-sm">
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
      <div className="mt-4">
        <Loading state={state}>
          {(mapping) =>
            mapping.role === role ? <Editor key={role} api={api} mapping={mapping} /> : <p className="text-sm">Loading…</p>
          }
        </Loading>
      </div>
    </Panel>
  );
}
