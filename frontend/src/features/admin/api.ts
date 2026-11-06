// The administrator console's calls to Web BFF (SRS 9.4). The services decide every one.
import { getJson, sendFile, sendJson } from "../../api/client";

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

export interface CustomDomain {
  domain: string;
  /** "pending" until it is proven, which a later module adds. */
  status: string;
  /** To publish in a DNS TXT record, as proof of control. */
  verification_token: string;
  created_at: string | null;
}

export interface BrandingFields {
  display_name: string;
  primary_color: string;
  accent_color: string;
  login_title: string;
  login_message: string;
  time_zone: string;
  locale: string;
}

export interface OwnBranding extends BrandingFields {
  tenant_id: string;
  has_logo: boolean;
  updated_at: string | null;
  custom_domain: CustomDomain | null;
}

export interface EmailTemplate {
  template: string;
  subject: string;
  body: string;
  customized: boolean;
  placeholders: string[];
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
    branding: () => getJson<OwnBranding>("/admin/branding", token),
    updateBranding: (key: string, fields: BrandingFields) =>
      sendJson<OwnBranding>("PUT", "/admin/branding", token, key, fields),
    setLogo: (key: string, file: File) => sendFile<OwnBranding>("/admin/branding/logo", token, key, file),
    templates: () => getJson<{ templates: EmailTemplate[] }>("/admin/email-templates", token),
    setTemplate: (key: string, template: string, subject: string, body: string) =>
      sendJson<EmailTemplate>("PUT", `/admin/email-templates/${encodeURIComponent(template)}`, token, key, {
        subject,
        body,
      }),
    setDomain: (key: string, domain: string) =>
      sendJson<CustomDomain>("PUT", "/admin/custom-domain", token, key, { domain }),
    removeDomain: (key: string) => sendJson<undefined>("DELETE", "/admin/custom-domain", token, key),
    denials: (pageToken = "") =>
      getJson<AccessDenials>(
        `/admin/access-denials?page_size=100&page_token=${encodeURIComponent(pageToken)}`,
        token,
      ),
  };
}

export type AdminApi = ReturnType<typeof adminApi>;
