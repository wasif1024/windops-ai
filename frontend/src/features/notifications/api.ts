// The notification center's calls to Web BFF (M11.6). A person only ever reads their own
// notifications; the tenant's rules need users.manage, and changing them a second factor. A
// notification holds identifiers and a link, never evidence (FR-NOT-002).
import { useCallback, useEffect, useState } from "react";
import { getJson, sendJson } from "../../api/client";

export interface Delivery {
  channel: "in_app" | "email";
  status: "delivered" | "pending" | "recorded" | "failed";
  attempts: number;
  updated_at: string | null;
}

export interface Notification {
  notification_id: string;
  kind: string;
  title: string;
  body: string;
  link: string;
  case_id: string;
  work_order_id: string;
  created_at: string | null;
  read_at: string | null;
  deliveries: Delivery[];
}

export interface NotificationList {
  notifications: Notification[];
  unread_count: number;
}

export interface Rule {
  kind: string;
  audience: string;
  channels: ("in_app" | "email")[];
}

export interface Rules {
  rules: Rule[];
  customized: boolean;
}

export const KINDS: Record<string, string> = {
  approval_requested: "Approval requested",
  decision_made: "Decision made",
  case_pending_review: "Case waits for review",
  case_approved: "Case approved",
  case_rejected: "Case rejected",
  case_needs_attention: "Case needs attention",
  case_verified: "Work verified",
};

export const AUDIENCES: Record<string, string> = {
  requester: "Who opened the case",
  drafter: "Who drafted the work order",
  submitter: "Who submitted the work order",
};

/** An audience in words: one of the named ones, or everyone who holds a permission. */
export function audienceText(audience: string): string {
  if (audience.startsWith("holders:")) return `Everyone with ${audience.slice("holders:".length)}`;
  return AUDIENCES[audience] ?? audience;
}

/** Whether an audience is one the Notification Service accepts, before it is sent. */
export function validAudience(kind: string, audience: string): boolean {
  if (audience.startsWith("holders:")) return /^holders:[a-z_]{1,40}\.[a-z_]{1,40}$/.test(audience);
  if (audience === "drafter" || audience === "submitter") return kind === "approval_requested" || kind === "decision_made";
  return audience === "requester";
}

/** Only a path in this console is followed, never another site. */
export function safeLink(link: string): string | null {
  return /^\/screens\/[a-z-]+(\?[A-Za-z0-9_=&%.-]*)?$/.test(link) ? link : null;
}

// Told when notifications are marked read here, so the badge in the shell follows at once.
export const CHANGED = "windops:notifications-changed";

export function notificationApi(token: string) {
  return {
    list: (unreadOnly: boolean) =>
      getJson<NotificationList>(`/notifications?page_size=100${unreadOnly ? "&unread_only=true" : ""}`, token),
    // Marking read is safe to repeat, so it needs no key of its own; the BFF still asks for one.
    markRead: async (ids: string[]) => {
      const answer = await sendJson<{ marked: number }>("POST", "/notifications/read", token, crypto.randomUUID(), {
        notification_ids: ids,
      });
      window.dispatchEvent(new Event(CHANGED));
      return answer;
    },
    rules: () => getJson<Rules>("/notifications/rules", token),
    setRules: (key: string, rules: Rule[]) => sendJson<Rules>("PUT", "/notifications/rules", token, key, { rules }),
  };
}

export type NotificationApi = ReturnType<typeof notificationApi>;

/** How many of the person's notifications are unread: read each minute, and whenever some are marked. */
export function useUnread(token: string, everyMs = 60_000): number {
  const [count, setCount] = useState(0);
  const read = useCallback(() => {
    getJson<NotificationList>("/notifications?unread_only=true&page_size=1", token)
      .then((answer) => setCount(answer.unread_count))
      .catch(() => undefined);
  }, [token]);
  useEffect(() => {
    read();
    const timer = window.setInterval(read, everyMs);
    window.addEventListener(CHANGED, read);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener(CHANGED, read);
    };
  }, [read, everyMs]);
  return count;
}
