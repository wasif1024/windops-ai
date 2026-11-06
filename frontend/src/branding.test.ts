import { describe, expect, it } from "vitest";
import { applyBranding, DEFAULTS, safeColor, signInTenant } from "./branding";

describe("branding", () => {
  it("uses only real hex colors, so nothing else can reach the stylesheet", () => {
    expect(safeColor("#123abc", DEFAULTS.primary)).toBe("#123abc");
    for (const bad of ["red", "#12345", "#123ABC;}body{display:none", "url(javascript:1)", undefined]) {
      expect(safeColor(bad, DEFAULTS.primary)).toBe(DEFAULTS.primary);
    }
  });

  it("applies a tenant's colors and name", () => {
    const root = document.createElement("div");
    applyBranding(
      {
        tenant_id: "north-sea", display_name: "North Sea Wind", primary_color: "#112233",
        accent_color: "bad", login_title: "", login_message: "", time_zone: "UTC", locale: "en-GB",
        has_logo: false,
      },
      root,
    );
    expect(root.style.getPropertyValue("--brand-primary")).toBe("#112233");
    expect(root.style.getPropertyValue("--brand-accent")).toBe(DEFAULTS.accent);
    expect(document.title).toBe("North Sea Wind · WindOps");
  });

  it("takes the sign-in tenant from the link, then remembers it", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    expect(signInTenant("?tenant=north-sea", storage)).toBe("north-sea");
    expect(signInTenant("", storage)).toBe("north-sea");
    expect(signInTenant("?tenant=../admin", storage)).toBe("north-sea");
    expect(signInTenant("", null)).toBeNull();
  });
});
