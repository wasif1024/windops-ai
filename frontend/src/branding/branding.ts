// A tenant's branding, applied as CSS variables. Values come from the API, so each is checked first.
import type { Branding } from "../api/client";

const COLOR = /^#[0-9a-f]{6}$/;
export const DEFAULTS = { primary: "#0b5394", accent: "#38761d" } as const;

export function safeColor(value: string | undefined, fallback: string): string {
  return value && COLOR.test(value) ? value : fallback;
}

export function applyBranding(branding: Branding | null, root: HTMLElement = document.documentElement): void {
  root.style.setProperty("--brand-primary", safeColor(branding?.primary_color, DEFAULTS.primary));
  root.style.setProperty("--brand-accent", safeColor(branding?.accent_color, DEFAULTS.accent));
  // Text, never HTML: React and document.title both escape it.
  document.title = branding?.display_name ? `${branding.display_name} · WindOps` : "WindOps";
  if (branding?.locale) document.documentElement.lang = branding.locale;
}

const TENANT_KEY = "windops.tenant";
const TENANT = /^[a-z0-9][a-z0-9-]{1,62}$/;

/** The tenant for the sign-in page: from ?tenant=, or the last one used. It is not a secret. */
export function signInTenant(search: string, storage: Pick<Storage, "getItem" | "setItem"> | null): string | null {
  const asked = new URLSearchParams(search).get("tenant");
  if (asked && TENANT.test(asked)) {
    try {
      storage?.setItem(TENANT_KEY, asked);
    } catch {
      // Storage can be blocked. The tenant still works for this visit.
    }
    return asked;
  }
  try {
    const remembered = storage?.getItem(TENANT_KEY) ?? null;
    return remembered && TENANT.test(remembered) ? remembered : null;
  } catch {
    return null;
  }
}

type Store = Pick<Storage, "getItem" | "setItem">;
const BRANDING_KEY = "windops.branding.";
const TEXT_FIELDS = ["display_name", "primary_color", "accent_color", "login_title", "login_message", "time_zone", "locale"] as const;

/** Keep a tenant's branding as last seen, for when the Tenant Service cannot be reached (SRS 7.7). */
export function rememberBranding(branding: Branding, storage: Store | null): void {
  if (!TENANT.test(branding.tenant_id)) return;
  try {
    storage?.setItem(BRANDING_KEY + branding.tenant_id, JSON.stringify(branding));
  } catch {
    // Storage can be blocked. The defaults are used in an outage instead.
  }
}

/**
 * The tenant's last known branding, or null. Only that tenant's, and only well-formed: what is
 * read back passes the same checks as an answer from the API. Its logo is served by the Tenant
 * Service too, so it is not shown.
 */
export function lastKnownBranding(tenantId: string, storage: Store | null): Branding | null {
  if (!TENANT.test(tenantId)) return null;
  let kept: unknown;
  try {
    kept = JSON.parse(storage?.getItem(BRANDING_KEY + tenantId) ?? "null");
  } catch {
    return null;
  }
  if (typeof kept !== "object" || kept === null) return null;
  const record = kept as Record<string, unknown>;
  if (record.tenant_id !== tenantId) return null;
  if (!TEXT_FIELDS.every((name) => typeof record[name] === "string")) return null;
  return { ...(record as unknown as Branding), has_logo: false };
}

/** The branding to show: the API's, remembered, or in an outage the last known one. */
export function brandingToShow(answer: Branding | null, tenantId: string, storage: Store | null): Branding | null {
  if (answer !== null) {
    rememberBranding(answer, storage);
    return answer;
  }
  return lastKnownBranding(tenantId, storage);
}
