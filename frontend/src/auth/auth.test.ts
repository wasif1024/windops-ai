import { InMemoryWebStorage } from "oidc-client-ts";
import { describe, expect, it } from "vitest";
import { settings } from "./auth";

describe("sign-in settings", () => {
  const config = { realm: "windops", clientId: "windops-web" };
  const value = settings(config, "https://localhost:9443");

  it("uses the code flow with the fixed issuer and the browser client", () => {
    expect(value.authority).toBe("https://localhost:9443/auth/realms/windops");
    expect(value.client_id).toBe("windops-web");
    expect(value.response_type).toBe("code");
    expect(value.redirect_uri).toBe("https://localhost:9443/callback");
  });

  it("can find Keycloak at the gateway while the page is on the dev server", () => {
    const dev = settings(config, "http://localhost:5173", "https://localhost:9443");
    expect(dev.authority).toBe("https://localhost:9443/auth/realms/windops");
    expect(dev.redirect_uri).toBe("http://localhost:5173/callback");
  });

  it("takes the realm and client from the settings", () => {
    const other = settings({ realm: "staging", clientId: "web" }, "https://windops.example.com");
    expect(other.authority).toBe("https://windops.example.com/auth/realms/staging");
    expect(other.client_id).toBe("web");
  });

  it("keeps tokens in memory only", () => {
    expect((value.userStore as unknown as { _store: unknown })._store).toBeInstanceOf(InMemoryWebStorage);
  });
});
