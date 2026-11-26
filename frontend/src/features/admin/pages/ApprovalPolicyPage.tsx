// The tenant's approval policy (RBAC-007, ADR 0033): the roles whose permissions include
// maintenance.approve, and who holds them where. Changing it is a change to a role's permissions
// or a grant of one of those roles, which waits below for a second administrator (ADR 0035).
import { useCallback } from "react";
import { Link } from "react-router-dom";
import { useLoad } from "../../../api/useLoad";
import { Loading, Panel, when } from "../../../components/Panel";
import { type AdminApi } from "../api";
import { ROLES, TENANT_ROLES } from "../catalog";
import { PolicyChangesPage } from "./PolicyChangesPage";

export const APPROVE = "maintenance.approve";

function roleTitle(id: string): string {
  return ROLES.find((role) => role.id === id)?.title ?? id;
}

/** Where a grant applies, in words. */
export function scopeText(windFarm: string, turbine: string): string {
  if (turbine) return `turbine ${turbine}`;
  if (windFarm) return `wind farm ${windFarm}`;
  return "the whole tenant";
}

export function ApprovalPolicyPage({ api, emails }: { api: AdminApi; emails: Map<string, string> | null }) {
  const load = useCallback(async () => {
    const roles = await Promise.all(TENANT_ROLES.map((role) => api.rolePermissions(role.id)));
    const approving = roles.filter((r) => r.permissions.includes(APPROVE));
    const { grants } = await api.allGrants();
    const names = new Set(approving.map((r) => r.role));
    return { approving, holders: grants.filter((g) => names.has(g.role) && !g.revoked_at) };
  }, [api]);
  const [state] = useLoad(load);
  return (
    <div className="space-y-4">
      <Panel title="Who may approve maintenance">
        <Loading state={state}>
          {({ approving, holders }) => (
            <div className="space-y-4 text-sm" data-testid="approval-policy">
              <p className="text-slate-600">
                A person may decide a work order when one of their roles includes {APPROVE} where it happens, and they had no
                hand in it. No agent ever approves.
              </p>
              <div>
                <h4 className="font-semibold text-slate-700">Roles that approve</h4>
                {approving.length === 0 ? (
                  <p className="text-amber-900">No role approves: nothing can be approved until one does.</p>
                ) : (
                  <ul className="mt-1 flex flex-wrap gap-2" aria-label="Roles that approve">
                    {approving.map((r) => (
                      <li key={r.role} className="rounded bg-slate-100 px-2 py-0.5">
                        {roleTitle(r.role)}
                        {r.customized && <span className="text-slate-500"> (changed here)</span>}
                      </li>
                    ))}
                  </ul>
                )}
                <Link to="?tab=roles" className="mt-1 inline-block text-brand-primary hover:underline">
                  Change role permissions
                </Link>
              </div>
              <div>
                <h4 className="font-semibold text-slate-700">People who hold them ({holders.length})</h4>
                {holders.length === 0 ? (
                  <p className="text-slate-500">Nobody holds an approving role.</p>
                ) : (
                  <div className="mt-1 overflow-x-auto">
                    <table className="w-full min-w-[30rem] text-left" data-testid="approvers">
                      <thead className="text-xs text-slate-500">
                        <tr>
                          <th className="py-1 font-medium">Person</th>
                          <th className="py-1 font-medium">Role</th>
                          <th className="py-1 font-medium">Where</th>
                          <th className="py-1 font-medium">Since</th>
                        </tr>
                      </thead>
                      <tbody>
                        {holders.map((g) => {
                          const email = emails?.get(g.user_id);
                          return (
                            <tr key={g.grant_id} className="border-t border-slate-100 align-top">
                              <td className="break-all py-1 pr-2">
                                {email ?? <span className="font-mono text-xs">{g.user_id}</span>}
                                {emails && !email && (
                                  <span className="ml-1 text-xs text-amber-900">no longer a user here: revoke it</span>
                                )}
                              </td>
                              <td className="py-1 pr-2">{roleTitle(g.role)}</td>
                              <td className="py-1 pr-2">{scopeText(g.wind_farm_id, g.turbine_id)}</td>
                              <td className="py-1 text-xs">{when(g.granted_at)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}
        </Loading>
      </Panel>
      <PolicyChangesPage api={api} title="Changes waiting for a second administrator" empty="No change to who may approve is waiting." />
    </div>
  );
}
