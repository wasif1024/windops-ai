import { useCallback, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi } from "../api";
import { useLoad } from "../../../api/useLoad";
import { BrandingPage } from "./BrandingPage";
import { DenialsPage } from "./DenialsPage";
import { RolesPage } from "./RolesPage";
import { UserPage } from "./UserPage";
import { UsersPage } from "./UsersPage";

interface Tab {
  id: string;
  title: string;
  /** Shown only to someone who holds this. The service checks it again on every call. */
  needs: string;
}

export const TABS: Tab[] = [
  { id: "users", title: "Users", needs: "users.manage" },
  { id: "roles", title: "Role permissions", needs: "permissions.manage" },
  { id: "denials", title: "Access denials", needs: "audit.read" },
  { id: "branding", title: "Branding", needs: "branding.manage" },
];

export function visibleTabs(held: ReadonlySet<string>): Tab[] {
  return TABS.filter((tab) => held.has(tab.needs));
}

/** The administrator console (SRS 9.4), within the signed-in user's own tenant. */
export function AdminPage({ token, held }: { token: string; held: ReadonlySet<string> }) {
  const api = useMemo(() => adminApi(token), [token]);
  const [params] = useSearchParams();
  const tabs = visibleTabs(held);
  const tab = tabs.find((item) => item.id === params.get("tab")) ?? tabs[0];
  const member = params.get("user");
  const canListUsers = held.has("users.manage");
  // Names for the IDs in the denials list, when this user may see the tenant's users.
  const [users] = useLoad(
    useCallback(async () => (canListUsers ? (await api.users()).users : []), [api, canListUsers]),
  );
  const emails = useMemo(
    () => new Map(users.kind === "ready" ? users.data.map((user) => [user.member_id, user.email]) : []),
    [users],
  );

  if (!tab) {
    return <p className="text-slate-600">Your roles include no part of the administrator console.</p>;
  }
  return (
    <section>
      <h2 className="text-xl font-semibold">Administration</h2>
      <nav aria-label="Administration" className="mt-3 -mx-4 overflow-x-auto px-4 md:mx-0 md:px-0">
        <ul className="flex gap-1 border-b border-slate-200">
          {tabs.map((item) => (
            <li key={item.id}>
              <Link
                to={`?tab=${item.id}`}
                aria-current={item.id === tab.id ? "page" : undefined}
                className={`block whitespace-nowrap px-3 py-2 text-sm font-medium ${
                  item.id === tab.id
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
        {tab.id === "users" &&
          (member ? (
            <UserPage api={api} id={member} canGrant={held.has("roles.manage")} />
          ) : (
            <UsersPage api={api} />
          ))}
        {tab.id === "roles" && <RolesPage api={api} />}
        {tab.id === "denials" && <DenialsPage api={api} emails={emails} />}
        {tab.id === "branding" && <BrandingPage api={api} />}
      </div>
    </section>
  );
}
