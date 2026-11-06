import { type FormEvent, useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { logoUrl } from "../../../api/client";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { type PlatformApi, SUSPEND_REASON_MINIMUM, type Tenant, type TenantBranding } from "../api";

function Lifecycle({ api, tenant, onChanged }: { api: PlatformApi; tenant: Tenant; onChanged: (t: Tenant) => void }) {
  const [reason, setReason] = useState("");
  const [short, setShort] = useState(false);
  const change = useChange();
  const active = tenant.status === "active";

  const suspend = async (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim().length < SUSPEND_REASON_MINIMUM) {
      setShort(true);
      return;
    }
    setShort(false);
    const result = await change.run((key) => api.suspend(key, tenant.tenant_id, reason.trim()));
    if (result) {
      setReason("");
      onChanged(result);
    }
  };
  const reactivate = async () => {
    const result = await change.run((key) => api.reactivate(key, tenant.tenant_id));
    if (result) onChanged(result);
  };

  return (
    <Panel title={tenant.name}>
      <dl className="grid gap-2 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="text-slate-500">Tenant ID</dt>
        <dd className="font-mono">{tenant.tenant_id}</dd>
        <dt className="text-slate-500">Status</dt>
        <dd data-testid="tenant-status">{active ? "Active" : "Suspended"}</dd>
        <dt className="text-slate-500">Created</dt>
        <dd>{when(tenant.created_at)}</dd>
      </dl>
      {active ? (
        <form onSubmit={(event) => void suspend(event)} className="mt-4 grid gap-2">
          <label className={LABEL}>
            Reason for suspending, kept with the tenant
            <input
              className={`${INPUT} mt-1`}
              maxLength={500}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          {short && (
            <p className="text-sm text-red-800">Give a reason of at least {SUSPEND_REASON_MINIMUM} characters.</p>
          )}
          <p className="text-xs text-slate-500">Its users can no longer sign in to it until it is reactivated.</p>
          <div>
            <Button type="submit" variant="outline" className="text-red-700" disabled={change.state.kind === "busy"}>
              Suspend tenant
            </Button>
          </div>
        </form>
      ) : (
        <Button className="mt-4" disabled={change.state.kind === "busy"} onClick={() => void reactivate()}>
          Reactivate tenant
        </Button>
      )}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </Panel>
  );
}

function Swatch({ label, color }: { label: string; color: string }) {
  return (
    <span className="flex items-center gap-2">
      <svg aria-hidden="true" viewBox="0 0 16 16" className="h-4 w-4 rounded border border-slate-300">
        <rect width="16" height="16" fill={color} />
      </svg>
      {label} <span className="font-mono">{color}</span>
    </span>
  );
}

/** Read-only: the tenant's own administrators change it (SRS 3.2). */
function Branding({ branding }: { branding: TenantBranding }) {
  return (
    <Panel title="Branding">
      <p className="text-xs text-slate-500">Only the tenant's own administrators change this.</p>
      <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-[10rem_1fr]">
        <dt className="text-slate-500">Name shown</dt>
        <dd>{branding.display_name || "—"}</dd>
        <dt className="text-slate-500">Sign-in title</dt>
        <dd>{branding.login_title || "—"}</dd>
        <dt className="text-slate-500">Colors</dt>
        <dd className="flex flex-wrap gap-4">
          <Swatch label="Primary" color={branding.primary_color} />
          <Swatch label="Accent" color={branding.accent_color} />
        </dd>
        <dt className="text-slate-500">Time zone, language</dt>
        <dd>
          {branding.time_zone}, {branding.locale}
        </dd>
        <dt className="text-slate-500">Custom domain</dt>
        <dd data-testid="tenant-domain">
          {branding.custom_domain ? `${branding.custom_domain.domain} (${branding.custom_domain.status})` : "None"}
        </dd>
        <dt className="text-slate-500">Logo</dt>
        <dd>
          {branding.has_logo ? (
            <img src={logoUrl(branding.tenant_id)} alt={`${branding.display_name} logo`} className="h-10 w-auto" />
          ) : (
            "None"
          )}
        </dd>
      </dl>
    </Panel>
  );
}

export function TenantPage({ api, id }: { api: PlatformApi; id: string }) {
  const [tenantState] = useLoad(useCallback(() => api.tenant(id), [api, id]));
  const [brandingState] = useLoad(useCallback(() => api.branding(id), [api, id]));
  const [updated, setUpdated] = useState<Tenant | null>(null);
  return (
    <div className="grid gap-4">
      <Link to="?tab=tenants" className="text-sm font-medium text-brand-primary">
        ← All tenants
      </Link>
      <Loading state={tenantState}>
        {(loaded) => (
          <Lifecycle api={api} tenant={updated?.tenant_id === id ? updated : loaded} onChanged={setUpdated} />
        )}
      </Loading>
      <Loading state={brandingState}>{(branding) => <Branding branding={branding} />}</Loading>
    </div>
  );
}
