// Sign-in with Keycloak: the authorization code flow with PKCE, through the gateway (SEC-001).
import {
  InMemoryWebStorage,
  type User,
  UserManager,
  WebStorageStateStore,
} from "oidc-client-ts";
import { createContext, type ReactNode, useContext, useEffect, useMemo, useState } from "react";

export const CLIENT_ID = "windops-web";
export const CALLBACK_PATH = "/callback";

export function settings(origin: string) {
  return {
    // The one fixed issuer, behind the same gateway as this page.
    authority: `${origin}/auth/realms/windops`,
    client_id: CLIENT_ID,
    redirect_uri: `${origin}${CALLBACK_PATH}`,
    post_logout_redirect_uri: `${origin}/`,
    response_type: "code",
    scope: "openid",
    // Tokens live in memory only, so script on the page cannot read them back from storage.
    userStore: new WebStorageStateStore({ store: new InMemoryWebStorage() }),
    // Refresh tokens are short-lived and used once each (SEC-001).
    automaticSilentRenew: true,
  };
}

interface Auth {
  user: User | null;
  ready: boolean;
  signIn: (options?: { acrValues?: string; returnTo?: string }) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const manager = useMemo(() => new UserManager(settings(window.location.origin)), []);
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const finish = async () => {
      if (window.location.pathname === CALLBACK_PATH) {
        const signedIn = await manager.signinRedirectCallback();
        const returnTo = typeof signedIn.state === "string" ? signedIn.state : "/";
        // Only a path on this site, so a crafted link cannot send the user elsewhere.
        window.history.replaceState(null, "", returnTo.startsWith("/") ? returnTo : "/");
        if (!cancelled) setUser(signedIn);
      } else {
        const current = await manager.getUser();
        if (!cancelled) setUser(current && !current.expired ? current : null);
      }
    };
    finish()
      .catch(() => {
        if (!cancelled) setUser(null);
      })
      .finally(() => {
        if (!cancelled) setReady(true);
      });
    const loaded = (next: User) => setUser(next);
    const gone = () => setUser(null);
    manager.events.addUserLoaded(loaded);
    manager.events.addUserUnloaded(gone);
    manager.events.addSilentRenewError(gone);
    return () => {
      cancelled = true;
      manager.events.removeUserLoaded(loaded);
      manager.events.removeUserUnloaded(gone);
      manager.events.removeSilentRenewError(gone);
    };
  }, [manager]);

  const value = useMemo<Auth>(
    () => ({
      user,
      ready,
      signIn: async (options) => {
        await manager.signinRedirect({
          state: options?.returnTo ?? window.location.pathname,
          ...(options?.acrValues ? { acr_values: options.acrValues } : {}),
        });
      },
      signOut: async () => {
        await manager.signoutRedirect();
      },
    }),
    [manager, user, ready],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth needs an AuthProvider");
  return auth;
}
