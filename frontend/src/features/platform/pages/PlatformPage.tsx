import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { platformApi } from "../api";
import { BreakGlassPage } from "./BreakGlassPage";
import { TenantPage } from "./TenantPage";
import { TenantsPage } from "./TenantsPage";

const TABS = [
  { id: "tenants", title: "Tenants" },
  { id: "break-glass", title: "Break-glass access" },
];

/** The Platform Administrator's console, across tenants (M3.10). */
export function PlatformPage({ token }: { token: string }) {
  const api = useMemo(() => platformApi(token), [token]);
  const [params] = useSearchParams();
  const tab = TABS.find((item) => item.id === params.get("tab")) ?? TABS[0];
  const tenant = params.get("tenant");
  return (
    <section>
      <h2 className="text-xl font-semibold">Platform</h2>
      <nav aria-label="Platform" className="-mx-4 mt-3 overflow-x-auto px-4 md:mx-0 md:px-0">
        <ul className="flex gap-1 border-b border-slate-200">
          {TABS.map((item) => (
            <li key={item.id}>
              <Link
                to={`?tab=${item.id}`}
                aria-current={item.id === tab?.id ? "page" : undefined}
                className={`block whitespace-nowrap px-3 py-2 text-sm font-medium ${
                  item.id === tab?.id
                    ? "border-b-2 border-brand-primary text-brand-primary"
                    : "text-slate-600 hover:text-slate-900"
                }`}
              >
                {item.title}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <div className="mt-4">
        {tab?.id === "tenants" && (tenant ? <TenantPage api={api} id={tenant} /> : <TenantsPage api={api} />)}
        {tab?.id === "break-glass" && <BreakGlassPage api={api} />}
      </div>
    </section>
  );
}
