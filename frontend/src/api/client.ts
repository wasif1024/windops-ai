// Calls to Web BFF, which answers with RFC 9457 problems that carry a stable code.

export const API = "/api/v1";

export interface Problem {
  title: string;
  status: number;
  code?: string;
}

export class ApiError extends Error {
  constructor(readonly problem: Problem) {
    super(problem.title);
  }

  /** The service wants a recent second factor (RFC 9470), so ask for one and try again. */
  get needsStepUp(): boolean {
    return this.problem.code === "step_up_required";
  }
}

export interface Branding {
  tenant_id: string;
  display_name: string;
  primary_color: string;
  accent_color: string;
  login_title: string;
  login_message: string;
  time_zone: string;
  locale: string;
  has_logo: boolean;
}

export interface Me {
  user_id: string;
  tenant_id: string;
  email: string;
  client: string;
  auth_level: string | null;
}

export interface ScopedPermission {
  permission: string;
  wind_farm_id: string;
  turbine_id: string;
}

export interface Permissions {
  roles: { role: string; wind_farm_id: string; turbine_id: string }[];
  permissions: ScopedPermission[];
}

async function problemOf(response: Response): Promise<Problem> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "title" in body) return body as Problem;
  } catch {
    // Not a problem document, such as the gateway's own refusal.
  }
  return { title: response.statusText || "Request failed", status: response.status };
}

export async function getJson<T>(path: string, token?: string, fetcher: typeof fetch = fetch): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetcher(`${API}${path}`, { headers, credentials: "omit" });
  if (!response.ok) throw new ApiError(await problemOf(response));
  return (await response.json()) as T;
}

export function logoUrl(tenant: string): string {
  return `${API}/public/branding/${encodeURIComponent(tenant)}/logo`;
}
