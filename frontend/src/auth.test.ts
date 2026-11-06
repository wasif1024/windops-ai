import { InMemoryWebStorage } from "oidc-client-ts";
import { describe, expect, it } from "vitest";
import { settings } from "./auth";

describe("sign-in settings", () => {
  const value = settings("https://localhost:9443");

  it("uses the code flow with the fixed issuer and the browser client", () => {
    expect(value.authority).toBe("https://localhost:9443/auth/realms/windops");
    expect(value.client_id).toBe("windops-web");
    expect(value.response_type).toBe("code");
    expect(value.redirect_uri).toBe("https://localhost:9443/callback");
  });

  it("keeps tokens in memory only", () => {
    expect((value.userStore as unknown as { _store: unknown })._store).toBeInstanceOf(InMemoryWebStorage);
  });
});
