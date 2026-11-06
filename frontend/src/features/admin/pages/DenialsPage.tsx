import { useCallback } from "react";
import { Button } from "../../../components/Button";
import type { AccessDenial, AdminApi } from "../api";
import { Loading, Panel, when } from "../../../components/Panel";
import { useLoad } from "../../../api/useLoad";

// The trail is read oldest first, so this many pages are read to show the newest at the top.
const MAXIMUM_PAGES = 10;

interface Shown {
  denials: AccessDenial[];
  /** True when the trail holds more than were read. */
  more: boolean;
}

/** Refused attempts in this tenant, from the audit trail (SRS 9.4), newest first. */
export function DenialsPage({ api, emails }: { api: AdminApi; emails: Map<string, string> }) {
  const load = useCallback(async (): Promise<Shown> => {
    let page = await api.denials();
    const all = [...page.denials];
    for (let read = 1; page.next_page_token && read < MAXIMUM_PAGES; read += 1) {
      page = await api.denials(page.next_page_token);
      all.push(...page.denials);
    }
    return { denials: all.reverse(), more: Boolean(page.next_page_token) };
  }, [api]);
  const [state, reload] = useLoad(load);

  return (
    <Panel
      title="Access denials"
      actions={
        <Button variant="outline" className="text-brand-primary" onClick={reload}>
          Refresh
        </Button>
      }
    >
      <Loading state={state}>
        {({ denials, more }) =>
          denials.length === 0 ? (
            <p className="text-sm text-slate-500">No refused attempts.</p>
          ) : (
            <>
              {more && (
                <p className="mb-2 text-xs text-amber-800">
                  Showing the oldest {denials.length}. The newest need the audit explorer, which arrives in Phase 11.
                </p>
              )}
              <ul className="divide-y divide-slate-100 text-sm" aria-label="Access denials">
                <li className="hidden pb-2 text-xs font-medium uppercase text-slate-500 md:grid md:grid-cols-[12rem_2fr_1fr]">
                  <span>When</span>
                  <span>Who</span>
                  <span>Permission</span>
                </li>
                {denials.map((denial) => (
                  <li key={denial.sequence} className="grid gap-1 py-2 md:grid-cols-[12rem_2fr_1fr]">
                    <span className="text-slate-500">{when(denial.occurred_at)}</span>
                    <span className="break-all">{emails.get(denial.user_id) ?? denial.user_id}</span>
                    <span className="font-mono">{denial.permission}</span>
                  </li>
                ))}
              </ul>
            </>
          )
        }
      </Loading>
    </Panel>
  );
}
