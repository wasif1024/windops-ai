// The Platform console's calls to Web BFF (M3.10). The services decide every one: the Tenant
// Service needs tenant.manage, and Authorization allows break-glass only to a Platform Administrator.
import { getJson, sendJson } from "../../api/client";

export interface Tenant {
  tenant_id: string;
  name: string;
  /** "active" or "suspended". */
  status: string;
  created_at: string | null;
}

export interface TenantBranding {
  tenant_id: string;
  display_name: string;
  primary_color: string;
  accent_color: string;
  login_title: string;
  login_message: string;
  time_zone: string;
  locale: string;
  has_logo: boolean;
  updated_at: string | null;
  /** Without its proof token, which is the tenant's own. */
  custom_domain: { domain: string; status: string } | null;
}

export interface BreakGlassGrant {
  grant_id: string;
  tenant_id: string;
  reason: string;
  started_at: string | null;
  expires_at: string | null;
  ended_at: string | null;
}

const tenant = (id: string) => `/platform/tenants/${encodeURIComponent(id)}`;

/** Every call, bound to the signed-in Platform Administrator's token. */
export function platformApi(token: string) {
  return {
    tenants: (pageToken = "") =>
      getJson<{ tenants: Tenant[]; next_page_token: string }>(
        `/platform/tenants?page_size=200&page_token=${encodeURIComponent(pageToken)}`,
        token,
      ),
    createTenant: (key: string, tenantId: string, name: string) =>
      sendJson<{ tenant: Tenant; created: boolean }>("POST", "/platform/tenants", token, key, {
        tenant_id: tenantId,
        name,
      }),
    tenant: (id: string) => getJson<Tenant>(tenant(id), token),
    suspend: (key: string, id: string, reason: string) =>
      sendJson<Tenant>("POST", `${tenant(id)}/suspend`, token, key, { reason }),
    reactivate: (key: string, id: string) => sendJson<Tenant>("POST", `${tenant(id)}/reactivate`, token, key),
    branding: (id: string) => getJson<TenantBranding>(`${tenant(id)}/branding`, token),
    breakGlass: () => getJson<{ grants: BreakGlassGrant[] }>("/platform/break-glass", token),
    startBreakGlass: (key: string, tenantId: string, reason: string, minutes: number) =>
      sendJson<BreakGlassGrant>("POST", "/platform/break-glass", token, key, {
        tenant_id: tenantId,
        reason,
        duration_minutes: minutes,
      }),
    endBreakGlass: (key: string, grantId: string) =>
      sendJson<BreakGlassGrant>("POST", `/platform/break-glass/${encodeURIComponent(grantId)}/end`, token, key),
  };
}

export type PlatformApi = ReturnType<typeof platformApi>;

// The rules the services hold, so a mistake is shown before a round trip. They check again.
export const TENANT_ID = /^[a-z0-9][a-z0-9-]{1,62}$/;
export const SUSPEND_REASON_MINIMUM = 10;
export const BREAK_GLASS_REASON_MINIMUM = 20;
export const BREAK_GLASS_MINUTES = { minimum: 1, maximum: 240 };
