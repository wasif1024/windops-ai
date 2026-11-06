// The administrator console's calls to Web BFF (SRS 9.4). The services decide every one.
import { getJson, sendJson } from "../../api/client";

export interface User {
  member_id: string;
  email: string;
  first_name: string;
  last_name: string;
  enabled: boolean;
  created_at: string | null;
}

export interface UserPage {
  users: User[];
  next_page_token: string;
}

export interface CreatedUser {
  user: User;
  /** Only in the first answer. A resend with the same key has none. */
  temporary_password: string | null;
  already_created: boolean;
}

export interface Session {
  session_id: string;
  ip_address: string;
  started_at: string | null;
  last_access_at: string | null;
  clients: string[];
}

export interface MfaStatus {
  otp_configured: boolean;
  recovery_codes_configured: boolean;
  setup_required: boolean;
}

export interface RoleGrant {
  grant_id: string;
  user_id: string;
  role: string;
  wind_farm_id: string;
  turbine_id: string;
  granted_by: string;
  granted_at: string | null;
  revoked_by: string;
  revoked_at: string | null;
}

export interface RolePermissions {
  role: string;
  permissions: string[];
  customized: boolean;
}

export interface AccessDenial {
  sequence: number;
  occurred_at: string | null;
  user_id: string;
  permission: string;
}

export interface AccessDenials {
  denials: AccessDenial[];
  next_page_token: string;
}

const user = (id: string) => `/admin/users/${encodeURIComponent(id)}`;

/** Every call, bound to the signed-in user's token. */
export function adminApi(token: string) {
  return {
    users: (pageToken = "") =>
      getJson<UserPage>(`/admin/users?page_size=100&page_token=${encodeURIComponent(pageToken)}`, token),
    createUser: (key: string, body: { email: string; first_name: string; last_name: string }) =>
      sendJson<CreatedUser>("POST", "/admin/users", token, key, body),
    user: (id: string) => getJson<User>(user(id), token),
    disable: (key: string, id: string) => sendJson<User>("POST", `${user(id)}/disable`, token, key),
    reactivate: (key: string, id: string) => sendJson<User>("POST", `${user(id)}/reactivate`, token, key),
    sessions: (id: string) => getJson<{ sessions: Session[] }>(`${user(id)}/sessions`, token),
    revokeSessions: (key: string, id: string) =>
      sendJson<{ sessions_ended: number }>("POST", `${user(id)}/sessions/revoke`, token, key),
    mfa: (id: string) => getJson<MfaStatus>(`${user(id)}/mfa`, token),
    grants: (id: string) =>
      getJson<{ grants: RoleGrant[] }>(
        `/admin/role-grants?user_id=${encodeURIComponent(id)}&include_revoked=true`,
        token,
      ),
    grant: (key: string, body: { user_id: string; role: string; wind_farm_id: string; turbine_id: string }) =>
      sendJson<RoleGrant>("POST", "/admin/role-grants", token, key, body),
    revoke: (key: string, grantId: string) =>
      sendJson<RoleGrant>("POST", `/admin/role-grants/${encodeURIComponent(grantId)}/revoke`, token, key),
    rolePermissions: (role: string) =>
      getJson<RolePermissions>(`/admin/roles/${encodeURIComponent(role)}/permissions`, token),
    setRolePermissions: (key: string, role: string, permissions: string[]) =>
      sendJson<RolePermissions>("PUT", `/admin/roles/${encodeURIComponent(role)}/permissions`, token, key, {
        permissions,
      }),
    denials: (pageToken = "") =>
      getJson<AccessDenials>(
        `/admin/access-denials?page_size=100&page_token=${encodeURIComponent(pageToken)}`,
        token,
      ),
  };
}

export type AdminApi = ReturnType<typeof adminApi>;
