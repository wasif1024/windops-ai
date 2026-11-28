import { describe, expect, it } from "vitest";
import type { Branding } from "../api/client";
import { applyBranding, brandingToShow, DEFAULTS, lastKnownBranding, safeColor, signInTenant } from "./branding";

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

  describe("while the Tenant Service is down (SRS 7.7)", () => {
    const NORTH_SEA: Branding = {
      tenant_id: "north-sea", display_name: "North Sea Wind", primary_color: "#112233",
      accent_color: "#445566", login_title: "", login_message: "", time_zone: "UTC", locale: "en-GB",
      has_logo: true,
    };
    const memory = () => {
      const store = new Map<string, string>();
      return { store, storage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) } };
    };

    it("shows the last known branding, without its logo", () => {
      const { storage } = memory();
      expect(brandingToShow(NORTH_SEA, "north-sea", storage)).toEqual(NORTH_SEA);
      expect(brandingToShow(null, "north-sea", storage)).toEqual({ ...NORTH_SEA, has_logo: false });
    });

    it("never shows another tenant's branding", () => {
      const { storage } = memory();
      brandingToShow(NORTH_SEA, "north-sea", storage);
      expect(brandingToShow(null, "baltic", storage)).toBeNull();
    });

    it("ignores anything malformed or misfiled, and falls back to the defaults", () => {
      const { store, storage } = memory();
      store.set("windops.branding.north-sea", "{not json");
      expect(lastKnownBranding("north-sea", storage)).toBeNull();
      store.set("windops.branding.north-sea", JSON.stringify({ ...NORTH_SEA, tenant_id: "baltic" }));
      expect(lastKnownBranding("north-sea", storage)).toBeNull();
      store.set("windops.branding.north-sea", JSON.stringify({ ...NORTH_SEA, display_name: 7 }));
      expect(lastKnownBranding("north-sea", storage)).toBeNull();
      expect(lastKnownBranding("../admin", storage)).toBeNull();
    });

    it("works with storage blocked", () => {
      const blocked = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
      expect(brandingToShow(NORTH_SEA, "north-sea", blocked)).toEqual(NORTH_SEA);
      expect(brandingToShow(null, "north-sea", blocked)).toBeNull();
      expect(brandingToShow(null, "north-sea", null)).toBeNull();
    });
  });
});
