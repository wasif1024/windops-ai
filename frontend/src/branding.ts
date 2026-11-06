// A tenant's branding, applied as CSS variables. Values come from the API, so each is checked first.
import type { Branding } from "./api";

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
