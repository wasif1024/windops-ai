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

/** A new random key for one change, which the BFF and services use to recognise a resend (GRP-007). */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/** Send a change. The same key must go with every resend of the same change. */
export async function sendJson<T>(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  token: string,
  key: string,
  body?: unknown,
  fetcher: typeof fetch = fetch,
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    Authorization: `Bearer ${token}`,
    "Idempotency-Key": key,
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetcher(`${API}${path}`, {
    method,
    headers,
    credentials: "omit",
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) throw new ApiError(await problemOf(response));
  // A removal answers 204, with no body.
  return (response.status === 204 ? undefined : await response.json()) as T;
}

/** Send a file as the body, such as a logo, with its own media type. */
export async function sendFile<T>(
  path: string,
  token: string,
  key: string,
  file: Blob,
  fetcher: typeof fetch = fetch,
): Promise<T> {
  const response = await fetcher(`${API}${path}`, {
    method: "PUT",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      "Idempotency-Key": key,
      "Content-Type": file.type,
    },
    credentials: "omit",
    body: file,
  });
  if (!response.ok) throw new ApiError(await problemOf(response));
  return (await response.json()) as T;
}

export function logoUrl(tenant: string): string {
  return `${API}/public/branding/${encodeURIComponent(tenant)}/logo`;
}
