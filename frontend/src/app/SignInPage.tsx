import { useEffect, useState } from "react";
import { type Branding, getJson } from "../api/client";
import { useAuth } from "../auth/auth";
import { applyBranding, signInTenant } from "../branding/branding";
import { Button } from "../components/Button";
import { Logo } from "../components/Logo";

export function SignInPage() {
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
    <main className="mx-auto mt-[10vh] max-w-md rounded-lg border-t-4 border-brand-primary bg-white p-8 shadow-sm">
      <Logo branding={branding} />
      <h1 className="mt-4 text-2xl font-semibold">{branding?.login_title || "Sign in to WindOps"}</h1>
      {branding?.login_message ? <p className="mt-2 text-slate-600">{branding.login_message}</p> : null}
      <Button className="mt-6" onClick={() => void signIn({ returnTo: "/" })}>
        Sign in
      </Button>
    </main>
  );
}
