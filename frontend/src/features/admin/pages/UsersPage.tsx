import { type FormEvent, useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "../../../components/Button";
import type { AdminApi, CreatedUser, User } from "../api";
import { ChangeStatus } from "../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel } from "../components/Panel";
import { useChange } from "../useChange";
import { useLoad } from "../useLoad";

// The same simple check Identity makes, so most mistakes are caught before a round trip.
const EMAIL = /^[^@\s]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}$/;

function name(user: User): string {
  return [user.first_name, user.last_name].filter(Boolean).join(" ") || "—";
}

function AddUser({ api, onAdded }: { api: AdminApi; onAdded: (created: CreatedUser) => void }) {
  const [email, setEmail] = useState("");
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [invalid, setInvalid] = useState(false);
  const change = useChange();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const address = email.trim().toLowerCase();
    if (!EMAIL.test(address)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    const created = await change.run((key) =>
      api.createUser(key, { email: address, first_name: first.trim(), last_name: last.trim() }),
    );
    if (created) {
      setEmail("");
      setFirst("");
      setLast("");
      onAdded(created);
    }
  };

  return (
    <form onSubmit={(event) => void submit(event)} className="grid gap-3 md:grid-cols-[2fr_1fr_1fr_auto] md:items-end">
      <label className={LABEL}>
        Email
        <input
          className={`${INPUT} mt-1`}
          type="email"
          required
          maxLength={254}
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          aria-invalid={invalid}
          aria-describedby={invalid ? "email-error" : undefined}
        />
      </label>
      <label className={LABEL}>
        First name
        <input className={`${INPUT} mt-1`} maxLength={100} value={first} onChange={(e) => setFirst(e.target.value)} />
      </label>
      <label className={LABEL}>
        Last name
        <input className={`${INPUT} mt-1`} maxLength={100} value={last} onChange={(e) => setLast(e.target.value)} />
      </label>
      <Button type="submit" disabled={change.state.kind === "busy"}>
        Add user
      </Button>
      {invalid && (
        <p id="email-error" className="text-sm text-red-800 md:col-span-4">
          Enter an email address such as name@example.com.
        </p>
      )}
      <div className="md:col-span-4">
        <ChangeStatus state={change.state} onDismiss={change.reset} />
      </div>
    </form>
  );
}

/** Shown once: the service never gives the password again, and it is never stored here. */
function NewPassword({ created, onDone }: { created: CreatedUser; onDone: () => void }) {
  return (
    <div role="status" className="rounded-md border border-emerald-300 bg-emerald-50 p-4 text-sm text-emerald-900">
      <p className="font-medium">{created.user.email} was added.</p>
      {created.temporary_password ? (
        <>
          <p className="mt-1">
            Give them this temporary password privately. It is shown only now, and they must change it when they first
            sign in.
          </p>
          <code data-testid="temporary-password" className="mt-2 block break-all rounded bg-white px-3 py-2 font-mono">
            {created.temporary_password}
          </code>
        </>
      ) : (
        <p className="mt-1">They were already added, so there is no new password to show.</p>
      )}
      <Button className="mt-3" onClick={onDone}>
        Done
      </Button>
    </div>
  );
}

export function UsersPage({ api }: { api: AdminApi }) {
  const [extra, setExtra] = useState<User[]>([]);
  const [next, setNext] = useState("");
  const [created, setCreated] = useState<CreatedUser | null>(null);
  const load = useCallback(async () => {
    const page = await api.users();
    setExtra([]);
    setNext(page.next_page_token);
    return page.users;
  }, [api]);
  const [state, reload] = useLoad(load);

  const more = async () => {
    const page = await api.users(next);
    setExtra((users) => [...users, ...page.users]);
    setNext(page.next_page_token);
  };

  return (
    <div className="grid gap-4">
      <Panel title="Add a user">
        {created ? (
          <NewPassword created={created} onDone={() => setCreated(null)} />
        ) : (
          <AddUser
            api={api}
            onAdded={(user) => {
              setCreated(user);
              reload();
            }}
          />
        )}
      </Panel>
      <Panel title="Users">
        <Loading state={state}>
          {(users) => (
            <>
              <ul className="divide-y divide-slate-100" aria-label="Users">
                <li className="hidden px-2 pb-2 text-xs font-medium uppercase text-slate-500 md:grid md:grid-cols-[2fr_1fr_6rem]">
                  <span>Email</span>
                  <span>Name</span>
                  <span>Status</span>
                </li>
                {[...users, ...extra].map((user) => (
                  <li key={user.member_id}>
                    <Link
                      to={`?tab=users&user=${encodeURIComponent(user.member_id)}`}
                      className="grid gap-1 rounded px-2 py-3 hover:bg-slate-50 md:grid-cols-[2fr_1fr_6rem] md:items-center"
                    >
                      <span className="break-all font-medium text-brand-primary">{user.email}</span>
                      <span className="text-sm text-slate-600">{name(user)}</span>
                      <span className={`text-sm ${user.enabled ? "text-emerald-700" : "text-red-700"}`}>
                        {user.enabled ? "Active" : "Disabled"}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
              {next && (
                <Button variant="outline" className="mt-3 text-brand-primary" onClick={() => void more()}>
                  Show more
                </Button>
              )}
            </>
          )}
        </Loading>
      </Panel>
    </div>
  );
}
