import { useEffect, useRef, useState } from "react";
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
const MENU_ID = "screens-menu";

function ScreenLinks({ screens, onChoose }: { screens: Screen[]; onChoose?: () => void }) {
  return (
    <ul>
      {screens.map((screen) => (
        <li key={screen.id}>
          <NavLink
            to={`/screens/${screen.id}`}
            onClick={onChoose}
            className={({ isActive }) =>
              `block px-6 py-3 text-slate-700 hover:bg-slate-100 md:py-2 ${
                isActive ? "border-l-4 border-brand-accent font-semibold" : ""
              }`
            }
          >
            {screen.title}
          </NavLink>
        </li>
      ))}
    </ul>
  );
}

/** Below md, the menu slides over the page. It closes on Escape, a tap outside, or a choice. */
function MobileMenu({
  open,
  onClose,
  screens,
  email,
  onSignOut,
}: {
  open: boolean;
  onClose: () => void;
  screens: Screen[];
  email: string;
  onSignOut: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    panel.current?.querySelector("a")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 md:hidden">
      <button
        type="button"
        aria-label="Close the menu"
        className="absolute inset-0 bg-slate-900/40"
        onClick={onClose}
      />
      <div
        ref={panel}
        id={MENU_ID}
        role="dialog"
        aria-modal="true"
        aria-label="Screens"
        className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] flex-col bg-white py-4 shadow-xl"
      >
        <nav aria-label="Screens" className="flex-1 overflow-y-auto">
          <ScreenLinks screens={screens} onChoose={onClose} />
        </nav>
        <div className="border-t border-slate-200 px-6 pt-4">
          <p className="truncate text-sm text-slate-500" data-testid="menu-user-email">
            {email}
          </p>
          <Button className="mt-3 w-full" onClick={onSignOut}>
            Sign out
          </Button>
        </div>
      </div>
    </div>
  );
}

function Shell({ token }: { token: string }) {
  const { signOut } = useAuth();
  const session = useSession(token);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
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
  const closeMenu = () => {
    setMenuOpen(false);
    // Back to where the user was, so keyboard and screen reader users do not lose their place.
    menuButton.current?.focus();
  };
  return (
    <div className="min-h-screen md:grid md:grid-cols-[15rem_1fr] md:grid-rows-[auto_1fr]">
      <header className="flex items-center gap-3 bg-brand-primary px-4 py-3 text-white md:col-span-2 md:gap-4 md:px-6">
        <button
          ref={menuButton}
          type="button"
          className="-ml-1 rounded-md p-2 hover:bg-white/10 md:hidden"
          aria-label="Open the menu"
          aria-expanded={menuOpen}
          aria-controls={MENU_ID}
          onClick={() => setMenuOpen(true)}
        >
          <svg aria-hidden="true" viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor">
            <path strokeLinecap="round" strokeWidth="2" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
        <Logo branding={session.branding} />
        <strong className="truncate" data-testid="tenant-name">
          {session.branding?.display_name ?? session.me.tenant_id}
        </strong>
        <span className="ml-auto hidden truncate md:inline" data-testid="user-email">
          {session.me.email}
        </span>
        <Button variant="outline" className="hidden md:inline-block" onClick={() => void signOut()}>
          Sign out
        </Button>
      </header>
      <nav aria-label="Screens" className="hidden bg-white py-4 md:block">
        <ScreenLinks screens={session.screens} />
      </nav>
      <MobileMenu
        open={menuOpen}
        onClose={closeMenu}
        screens={session.screens}
        email={session.me.email}
        onSignOut={() => void signOut()}
      />
      <main className="min-w-0 p-4 md:p-6">
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
