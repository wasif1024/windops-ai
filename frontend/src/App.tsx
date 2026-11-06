import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Route, Routes, useParams } from "react-router-dom";
import { type Branding, getJson, logoUrl, type Me, type Permissions } from "./api";
import { useAuth } from "./auth";
import { applyBranding, signInTenant } from "./branding";
import { screenById, type Screen, visibleScreens } from "./navigation";

function Logo({ branding }: { branding: Branding | null }) {
  const [failed, setFailed] = useState(false);
  if (!branding?.has_logo || failed) return null;
  return (
    <img className="logo" src={logoUrl(branding.tenant_id)} alt="" onError={() => setFailed(true)} />
  );
}

function SignIn() {
  const { signIn } = useAuth();
  const [branding, setBranding] = useState<Branding | null>(null);
  const tenant = signInTenant(window.location.search, window.localStorage);

  useEffect(() => {
    if (!tenant) return;
    getJson<Branding>(`/public/branding/${encodeURIComponent(tenant)}`)
      .then(setBranding)
      .catch(() => setBranding(null));
  }, [tenant]);
  useEffect(() => applyBranding(branding), [branding]);

  return (
    <main className="sign-in">
      <Logo branding={branding} />
      <h1>{branding?.login_title || "Sign in to WindOps"}</h1>
      {branding?.login_message ? <p>{branding.login_message}</p> : null}
      <button type="button" onClick={() => void signIn({ returnTo: "/" })}>
        Sign in
      </button>
    </main>
  );
}

interface Session {
  me: Me;
  branding: Branding | null;
  screens: Screen[];
}

function useSession(token: string): Session | "loading" | "failed" {
  const [session, setSession] = useState<Session | "loading" | "failed">("loading");
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getJson<Me>("/me", token),
      getJson<Branding>("/branding", token).catch(() => null),
      getJson<Permissions>("/me/permissions", token),
    ])
      .then(([me, branding, permissions]) => {
        if (!cancelled) setSession({ me, branding, screens: visibleScreens(permissions.permissions) });
      })
      .catch(() => {
        if (!cancelled) setSession("failed");
      });
    return () => {
      cancelled = true;
    };
  }, [token]);
  return session;
}

function ScreenPage({ screens }: { screens: Screen[] }) {
  const { id } = useParams();
  const screen = screenById(id);
  if (!screen || !screens.includes(screen)) {
    return (
      <section>
        <h2>Not available</h2>
        <p>This screen does not exist, or your roles do not include it.</p>
      </section>
    );
  }
  return (
    <section>
      <h2>{screen.title}</h2>
      <p className="placeholder">This screen arrives in {screen.arrives}.</p>
    </section>
  );
}

function Shell({ token }: { token: string }) {
  const { signOut } = useAuth();
  const session = useSession(token);
  const branding = typeof session === "object" ? session.branding : null;
  useEffect(() => applyBranding(branding), [branding]);

  if (session === "loading") return <p className="status">Loading…</p>;
  if (session === "failed") {
    return (
      <main className="status">
        <p>Your account could not be loaded. Your tenant may be suspended.</p>
        <button type="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </main>
    );
  }
  const first = session.screens[0];
  return (
    <div className="shell">
      <header>
        <Logo branding={session.branding} />
        <strong className="tenant">{session.branding?.display_name ?? session.me.tenant_id}</strong>
        <span className="user">{session.me.email}</span>
        <button type="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </header>
      <nav aria-label="Screens">
        <ul>
          {session.screens.map((screen) => (
            <li key={screen.id}>
              <NavLink to={`/screens/${screen.id}`}>{screen.title}</NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main>
        <Routes>
          <Route path="/screens/:id" element={<ScreenPage screens={session.screens} />} />
          <Route path="*" element={first ? <Navigate to={`/screens/${first.id}`} replace /> : null} />
        </Routes>
      </main>
    </div>
  );
}

export function App() {
  const { user, ready } = useAuth();
  if (!ready) return <p className="status">Loading…</p>;
  return <BrowserRouter>{user ? <Shell token={user.access_token} /> : <SignIn />}</BrowserRouter>;
}
