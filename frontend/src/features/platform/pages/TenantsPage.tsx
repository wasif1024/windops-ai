import { type FormEvent, useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { type PlatformApi, TENANT_ID } from "../api";

function CreateTenant({ api, onCreated }: { api: PlatformApi; onCreated: (message: string) => void }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [invalid, setInvalid] = useState(false);
  const change = useChange();
  const create = async (event: FormEvent) => {
    event.preventDefault();
    const tenantId = id.trim().toLowerCase();
    if (!TENANT_ID.test(tenantId)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    const result = await change.run((key) => api.createTenant(key, tenantId, name.trim()));
    if (result) {
      setId("");
      setName("");
      onCreated(result.created ? `${tenantId} was created.` : `${tenantId} already existed, so nothing changed.`);
    }
  };
  return (
    <form onSubmit={(event) => void create(event)} className="grid gap-3 md:grid-cols-[1fr_2fr_auto] md:items-end">
      <label className={LABEL}>
        Tenant ID
        <input
          className={`${INPUT} mt-1 font-mono`}
          required
          maxLength={63}
          value={id}
          onChange={(event) => setId(event.target.value)}
          aria-invalid={invalid}
        />
      </label>
      <label className={LABEL}>
        Name
        <input
          className={`${INPUT} mt-1`}
          required
          maxLength={200}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </label>
      <Button type="submit" disabled={change.state.kind === "busy"}>
        Create tenant
      </Button>
      {invalid && (
        <p className="text-sm text-red-800 md:col-span-3">
          A tenant ID is lowercase letters, digits and hyphens, such as north-sea. It becomes part of every token.
        </p>
      )}
      <div className="md:col-span-3">
        <ChangeStatus state={change.state} onDismiss={change.reset} />
      </div>
    </form>
  );
}

export function TenantsPage({ api }: { api: PlatformApi }) {
  const [state, reload] = useLoad(useCallback(() => api.tenants(), [api]));
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <div className="grid gap-4">
      <Panel title="Create a tenant">
        <CreateTenant
          api={api}
          onCreated={(message) => {
            setNotice(message);
            reload();
          }}
        />
        {notice && (
          <p role="status" className="mt-3 text-sm text-emerald-700">
            {notice}
          </p>
        )}
      </Panel>
      <Panel title="Tenants">
        <Loading state={state}>
          {({ tenants }) => (
            <ul className="divide-y divide-slate-100" aria-label="Tenants">
              <li className="hidden px-2 pb-2 text-xs font-medium uppercase text-slate-500 md:grid md:grid-cols-[1fr_2fr_8rem_12rem]">
                <span>ID</span>
                <span>Name</span>
                <span>Status</span>
                <span>Created</span>
              </li>
              {tenants.map((tenant) => (
                <li key={tenant.tenant_id}>
                  <Link
                    to={`?tab=tenants&tenant=${encodeURIComponent(tenant.tenant_id)}`}
                    className="grid gap-1 rounded px-2 py-3 hover:bg-slate-50 md:grid-cols-[1fr_2fr_8rem_12rem] md:items-center"
                  >
                    <span className="font-mono font-medium text-brand-primary">{tenant.tenant_id}</span>
                    <span className="text-sm">{tenant.name}</span>
                    <span className={`text-sm ${tenant.status === "active" ? "text-emerald-700" : "text-red-700"}`}>
                      {tenant.status === "active" ? "Active" : "Suspended"}
                    </span>
                    <span className="text-sm text-slate-500">{when(tenant.created_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Loading>
      </Panel>
    </div>
  );
}
