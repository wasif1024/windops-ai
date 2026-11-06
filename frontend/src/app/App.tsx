import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";
import { type Branding, getJson, type Me, type Permissions } from "../api/client";
import { useAuth } from "../auth/auth";
import { applyBranding } from "../branding/branding";
import { Button } from "../components/Button";
import { Logo } from "../components/Logo";
import { type Screen, visibleScreens } from "./navigation";
import { ScreenPage } from "./ScreenPage";
import { SignInPage } from "./SignInPage";

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

const STATUS = "p-8 text-slate-500";

function Shell({ token }: { token: string }) {
  const { signOut } = useAuth();
  const session = useSession(token);
  const branding = typeof session === "object" ? session.branding : null;
  useEffect(() => applyBranding(branding), [branding]);

  if (session === "loading") return <p className={STATUS}>Loading…</p>;
  if (session === "failed") {
    return (
      <main className={STATUS}>
        <p>Your account could not be loaded. Your tenant may be suspended.</p>
        <Button className="mt-4" onClick={() => void signOut()}>
          Sign out
        </Button>
      </main>
    );
  }
  const first = session.screens[0];
  return (
    <div className="grid min-h-screen grid-cols-[15rem_1fr] grid-rows-[auto_1fr]">
      <header className="col-span-2 flex items-center gap-4 bg-brand-primary px-6 py-3 text-white">
        <Logo branding={session.branding} />
        <strong data-testid="tenant-name">{session.branding?.display_name ?? session.me.tenant_id}</strong>
        <span className="ml-auto" data-testid="user-email">
          {session.me.email}
        </span>
        <Button variant="outline" onClick={() => void signOut()}>
          Sign out
        </Button>
      </header>
      <nav aria-label="Screens" className="bg-white py-4">
        <ul>
          {session.screens.map((screen) => (
            <li key={screen.id}>
              <NavLink
                to={`/screens/${screen.id}`}
                className={({ isActive }) =>
                  `block px-6 py-2 text-slate-700 hover:bg-slate-100 ${
                    isActive ? "border-l-4 border-brand-accent font-semibold" : ""
                  }`
                }
              >
                {screen.title}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      <main className="p-6">
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
  if (!ready) return <p className={STATUS}>Loading…</p>;
  return <BrowserRouter>{user ? <Shell token={user.access_token} /> : <SignInPage />}</BrowserRouter>;
}
