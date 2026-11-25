# ADR 0037: Notifications from events, by tenant rules, with email recorded

**Status:** Accepted, 2026-11-25, for M10.5.

## Context

The requirements:
- **FR-NOT-001:** deliver configured email and in-app notifications, with delivery status.
- **FR-NOT-002:** send case links or identifiers, never sensitive evidence, secrets or unnecessary personal data.
- **SRS 5.4 step 14:** notify users once a decision resumes the case.
- **LLM-010:** a run that exhausts its budget notifies a human.

SRS 7.7 sets the failure modes:
- **Notification is down:** events queue in JetStream, and the workflow continues.
- **The email provider is down:** in-app notifications are still delivered.

Already in place:
- **The events:** `workorder.submitted`, `approval.decided` and `case.updated` carry the identifiers needed. Since
  ADR 0036, `case.updated` also carries each move a decision makes.
- **Who may do what:** only Authorization knows, and services act as themselves with fixed, read-only grants
  (ADR 0008).

No email provider exists, and the user chose not to run one locally. Email is rendered and recorded, never sent.

## Decision

**The Notification Service**, with its own database, listens on three durable consumers:
`notification-submissions`, `notification-decisions` and `notification-cases`.

**Hearing and telling are separate.**
- **Hearing:** a consumer keeps each event once, by its ID, as identifiers only, and never calls another service.
- **Telling:** a dispatcher works out whom to tell, makes the notifications and sends their email.
- **When a dependency is down:** an Authorization or Asset outage only delays notifications. Each is tried again, sooner
  then later, up to every five minutes.
- **Old events:** an event that happened more than a day before it was heard, or that nobody could be told about
  within a day, tells nobody. It is kept as skipped, with the reason, so replayed history never floods people.

**Kinds and default audiences:**

| Kind | From | Default audience |
| --- | --- | --- |
| `approval_requested` | `workorder.submitted` | Holders of `maintenance.approve` where the turbine is, never its drafter or submitter (RBAC-004) |
| `decision_made` | `approval.decided` | Its drafter and submitter, when they are people |
| `case_pending_review` | `case.updated`, investigation completed | The person who opened the case |
| `case_approved`, `case_rejected` | `case.updated`, a decision resumed it | The person who opened the case |
| `case_needs_attention` | `case.updated`, its run ended otherwise (LLM-010) or a resume stopped (AGT-005) | The person who opened the case |

- **A case from detections** has no requester. Its requester is everyone holding `finding.review` there.
- **`case.updated` gains `requested_by`:** the user who opened the case, or nothing for one from detections.
- **Channels:** every default rule uses both in-app and email.
- **A tenant's own rules** replace the defaults:
  - `SetNotificationRules` needs `users.manage`, is idempotent by key (GRP-007), and is audited;
  - an empty list goes back to the defaults;
  - an audience is `requester`, `drafter`, `submitter` or `holders:<permission>`.

**Who holds a permission is asked of Authorization.** A new read-only call, `ListPermissionHolders`, answers with user
IDs only.
- **The scope** works as `Check`'s does: a wind farm grant covers a turbine only when both are given. Notification
  looks the farm up in Asset, with a new fixed `turbine.read` grant.
- **Callers:** only the Notification Service acting as itself, never an agent, and people with `roles.manage`.
- **Why not `ListRoleGrants`:** it needs `roles.manage`, a change permission no service may hold. It would also leave
  Notification to repeat the role-permission mapping and its custom versions.

**What a notification says** (FR-NOT-002):
- **Content:** a title and body built from identifiers (work order and version, case, turbine), and a link to a
  console screen.
- **Never:** a decision's reason, evidence, a name or an address. Deliveries name recipients by user ID only.

**Delivery** (FR-NOT-001): each notification has a delivery for each channel.
- **In-app:** delivered as it is made, in the same transaction.
- **Email:** waits for its sender. The only sender, `record` (`WINDOPS_NOTIFICATION_EMAIL`), renders the email and marks
  it `recorded`. A sender error is tried five times, then `failed`, while in-app stays delivered.
- **A real provider later** goes behind the same interface, such as Azure Communication Services in Phase 15. It would
  look the address up as it sends, so this service still never keeps one.

**The API:**
- **`ListMyNotifications` and `MarkNotificationsRead`:** a person's own notifications, never an agent's or a
  service's.
- **`ListDeliveries`:** every delivery in the tenant, for `audit.read`.
- **`GetNotificationRules` and `SetNotificationRules`:** need `users.manage`.
- **Web BFF:** serves `GET /api/v1/notifications` and `POST /api/v1/notifications/read`. The notification center
  screen comes in M11.6.

## Consequences

- **The check:** `make notification-check` proves it with the real services:
  - a submission reaches a manager and not its submitter;
  - the decision reaches the planner, and the case's approval reaches the engineer who opened it;
  - each is delivered in-app, with its email recorded;
  - no reason, evidence or address appears in any of them;
  - people see and mark only their own;
  - an auditor sees the deliveries by user ID, and a planner may not.
- **Email is never sent.** "Recorded" says so plainly. The tenant email templates (FR-TEN-002) apply once a real
  sender exists.
- **Every holder is told.** With many managers on a large tenant, each gets one notification per submission.
- **Revisit if:** a tenant needs per-person preferences or quiet hours, or a real email provider is chosen.
