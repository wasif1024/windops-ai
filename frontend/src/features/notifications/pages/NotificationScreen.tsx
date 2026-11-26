// The notification center (SRS 4.11, M11.6): the person's own notifications, newest first, each
// with whether it reached them in the app and by email, opened or marked read. With users.manage,
// the tenant's rules too: whom each kind goes to, and how. A notification says what happened and
// where to look, never the evidence itself (FR-NOT-002).
import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import {
  audienceText,
  KINDS,
  type Notification,
  type NotificationApi,
  notificationApi,
  type Rule,
  safeLink,
  validAudience,
} from "../api";

const DELIVERY_STYLE: Record<string, string> = {
  delivered: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  recorded: "bg-slate-100 text-slate-700 ring-1 ring-slate-200",
  pending: "bg-sky-50 text-sky-800 ring-1 ring-sky-200",
  failed: "bg-red-50 text-red-800 ring-1 ring-red-200",
};

/** Where a notification leads: its screen, opened on its case when it names one. */
function destination(n: Notification): string | null {
  const link = safeLink(n.link);
  if (!link) return null;
  if (link === "/screens/investigations" && n.case_id) return `${link}?case=${encodeURIComponent(n.case_id)}`;
  return link;
}

function Inbox({ api }: { api: NotificationApi }) {
  const navigate = useNavigate();
  const [unreadOnly, setUnreadOnly] = useState(false);
  const load = useCallback(() => api.list(unreadOnly), [api, unreadOnly]);
  const [state, reload] = useLoad(load);
  const change = useChange();
  const mark = (ids: string[]) => void change.run(() => api.markRead(ids)).then((done) => done && reload());
  return (
    <Panel
      title="Your notifications"
      actions={
        <div className="flex flex-wrap gap-2" role="group" aria-label="Show">
          {[false, true].map((only) => (
            <button
              key={String(only)}
              type="button"
              aria-pressed={unreadOnly === only}
              onClick={() => setUnreadOnly(only)}
              className={`rounded-md px-3 py-1 text-sm ring-1 ${unreadOnly === only ? "bg-sky-50 ring-sky-300" : "ring-slate-200 hover:bg-slate-50"}`}
            >
              {only ? "Unread" : "All"}
            </button>
          ))}
        </div>
      }
    >
      <Loading state={state}>
        {({ notifications, unread_count }) => (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <p data-testid="unread-count">{unread_count === 1 ? "1 unread" : `${unread_count} unread`}</p>
              {unread_count > 0 && (
                <Button variant="outline" className="text-brand-primary" onClick={() => mark([])} disabled={change.state.kind === "busy"}>
                  Mark all read
                </Button>
              )}
            </div>
            <ChangeStatus state={change.state} onDismiss={change.reset} />
            {notifications.length === 0 ? (
              <p className="text-sm text-slate-500">{unreadOnly ? "Nothing unread." : "No notifications yet."}</p>
            ) : (
              <ul className="divide-y divide-slate-100" aria-label="Notifications" data-testid="notifications">
                {notifications.map((n) => {
                  const to = destination(n);
                  return (
                    <li key={n.notification_id} className="py-3" data-unread={n.read_at ? undefined : "true"}>
                      <div className="flex flex-wrap items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className={`break-words ${n.read_at ? "text-slate-700" : "font-semibold text-slate-900"}`}>
                            {!n.read_at && (
                              <span aria-hidden="true" className="mr-2 inline-block h-2 w-2 rounded-full bg-brand-accent align-middle" />
                            )}
                            {n.title}
                            {!n.read_at && <span className="sr-only"> (unread)</span>}
                          </p>
                          <p className="mt-0.5 break-words text-sm text-slate-600">{n.body}</p>
                          <p className="mt-1 text-xs text-slate-500">
                            {KINDS[n.kind] ?? n.kind} · {when(n.created_at)}
                            {n.read_at && ` · read ${when(n.read_at)}`}
                          </p>
                          <div className="mt-1 flex flex-wrap gap-1" aria-label="Deliveries">
                            {n.deliveries.map((d) => (
                              <StatusBadge
                                key={d.channel}
                                status={`${d.channel === "email" ? "Email" : "In app"}: ${d.status}`}
                                className={DELIVERY_STYLE[d.status] ?? ""}
                              />
                            ))}
                          </div>
                        </div>
                        <div className="flex shrink-0 flex-wrap gap-2">
                          {to && (
                            <Button
                              onClick={() => {
                                if (!n.read_at) void api.markRead([n.notification_id]).catch(() => undefined);
                                navigate(to);
                              }}
                            >
                              Open
                            </Button>
                          )}
                          {!n.read_at && (
                            <Button variant="outline" className="text-brand-primary" onClick={() => mark([n.notification_id])}>
                              Mark read
                            </Button>
                          )}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}
      </Loading>
    </Panel>
  );
}

function RuleRow({ rule, onChange, onRemove }: { rule: Rule; onChange: (r: Rule) => void; onRemove: () => void }) {
  const fine = validAudience(rule.kind, rule.audience) && rule.channels.length > 0;
  return (
    <li className="grid gap-2 py-3 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-center" data-testid="rule">
      <select aria-label="Kind" className={INPUT} value={rule.kind} onChange={(e) => onChange({ ...rule, kind: e.target.value })}>
        {Object.entries(KINDS).map(([kind, label]) => (
          <option key={kind} value={kind}>
            {label}
          </option>
        ))}
      </select>
      <div>
        <input
          aria-label="Audience"
          className={INPUT}
          list="notification-audiences"
          value={rule.audience}
          onChange={(e) => onChange({ ...rule, audience: e.target.value.trim() })}
        />
        <p className={`mt-0.5 text-xs ${fine ? "text-slate-500" : "text-red-800"}`}>
          {validAudience(rule.kind, rule.audience) ? audienceText(rule.audience) : "Not an audience for this kind"}
        </p>
      </div>
      <fieldset className="flex gap-3 text-sm">
        <legend className="sr-only">Channels</legend>
        {(["in_app", "email"] as const).map((channel) => (
          <label key={channel} className="inline-flex items-center gap-1">
            <input
              type="checkbox"
              checked={rule.channels.includes(channel)}
              onChange={(e) =>
                onChange({
                  ...rule,
                  channels: e.target.checked ? [...rule.channels, channel] : rule.channels.filter((c) => c !== channel),
                })
              }
            />
            {channel === "email" ? "Email" : "In app"}
          </label>
        ))}
      </fieldset>
      <Button variant="outline" className="text-red-800" onClick={onRemove}>
        Remove
      </Button>
    </li>
  );
}

function RulesPanel({ api }: { api: NotificationApi }) {
  const load = useCallback(() => api.rules(), [api]);
  const [state, reload] = useLoad(load);
  const [draft, setDraft] = useState<Rule[] | null>(null);
  const change = useChange();
  return (
    <Panel title="Who is told">
      <Loading state={state}>
        {(saved) => {
          const rules = draft ?? saved.rules;
          const seen = new Set(rules.map((r) => `${r.kind} ${r.audience}`));
          const fine =
            seen.size === rules.length && rules.every((r) => validAudience(r.kind, r.audience) && r.channels.length > 0);
          const save = (next: Rule[]) =>
            void change.run((key) => api.setRules(key, next)).then((done) => {
              if (done) {
                setDraft(null);
                reload();
              }
            });
          return (
            <div className="space-y-3 text-sm" data-testid="notification-rules">
              <p className="text-slate-600">
                {saved.customized ? "This tenant's own rules." : "The defaults: this tenant has not changed them."} Each kind
                goes to its audience, in the app, by email, or both. A notification never holds evidence or anyone's details.
              </p>
              <datalist id="notification-audiences">
                {["requester", "drafter", "submitter"].map((a) => (
                  <option key={a} value={a} />
                ))}
                <option value="holders:maintenance.approve" />
                <option value="holders:finding.review" />
              </datalist>
              <ul className="divide-y divide-slate-100" aria-label="Rules">
                {rules.map((rule, index) => (
                  <RuleRow
                    key={index}
                    rule={rule}
                    onChange={(next) => setDraft(rules.map((r, i) => (i === index ? next : r)))}
                    onRemove={() => setDraft(rules.filter((_, i) => i !== index))}
                  />
                ))}
              </ul>
              {seen.size !== rules.length && <p className="text-red-800">Each kind and audience at most once.</p>}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  className="text-brand-primary"
                  onClick={() => setDraft([...rules, { kind: "case_pending_review", audience: "requester", channels: ["in_app"] }])}
                >
                  Add a rule
                </Button>
                <Button onClick={() => save(rules)} disabled={!draft || !fine || change.state.kind === "busy"}>
                  Save the rules
                </Button>
                {draft && (
                  <Button variant="outline" className="text-slate-700" onClick={() => setDraft(null)}>
                    Discard changes
                  </Button>
                )}
                {saved.customized && (
                  <Button variant="outline" className="text-slate-700" onClick={() => save([])} disabled={change.state.kind === "busy"}>
                    Go back to the defaults
                  </Button>
                )}
              </div>
              <ChangeStatus state={change.state} onDismiss={change.reset} />
            </div>
          );
        }}
      </Loading>
    </Panel>
  );
}

export function NotificationScreen({ token, held }: { token: string; held: ReadonlySet<string> }) {
  const api = useMemo(() => notificationApi(token), [token]);
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Notifications</h2>
      <Inbox api={api} />
      {held.has("users.manage") && <RulesPanel api={api} />}
      {held.has("audit.read") && (
        <p className="text-sm text-slate-500">
          What reached everyone in the tenant is in the{" "}
          <Link to="/screens/audit?tab=deliveries" className="text-brand-primary hover:underline">
            audit explorer
          </Link>
          .
        </p>
      )}
    </section>
  );
}
