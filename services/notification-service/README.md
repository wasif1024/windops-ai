# windops-notification

Tells people what needs them, in the app and by email, and keeps whether each was delivered (FR-NOT-001). A
notification carries identifiers and a link, never evidence, a decision's reason, a name or an address
(FR-NOT-002). The decisions behind it are in
[ADR 0037](../../docs/adr/0037-notifications-from-events-by-tenant-rules-with-email-recorded.md).

## What tells whom

| Kind | From | By default, to |
| --- | --- | --- |
| `approval_requested` | `workorder.submitted` | Holders of `maintenance.approve` where the turbine is, never its drafter or submitter |
| `decision_made` | `approval.decided` | Its drafter and submitter, when people |
| `case_pending_review`, `case_approved`, `case_rejected`, `case_needs_attention`, `case_verified` | `case.updated` | The person who opened the case, or for one from detections, holders of `finding.review` there |

A tenant's administrator (`users.manage`, with a recent second factor, SEC-003) may replace the defaults with
`SetNotificationRules`; an empty list goes back to them. The console's notification center sets them (M11.6).

## How it works

- **Hearing:** each of the three events is kept once, by its ID, as identifiers only. One that happened over a day
  before it was heard tells nobody, and is kept as skipped.
- **Telling:** a dispatcher, every two seconds, asks Authorization who holds a permission (`ListPermissionHolders`,
  as this service) and Asset for the turbine's wind farm. An outage only delays it.
- **Delivering:** in-app at once. Email through the sender `WINDOPS_NOTIFICATION_EMAIL`: `record`, the only one,
  renders it and marks it `recorded`, sending nothing. A failing sender is tried five times.

## API

| Call | Who |
| --- | --- |
| `ListMyNotifications`, `MarkNotificationsRead` | A person, for their own |
| `ListDeliveries` | `audit.read` |
| `GetNotificationRules` | `users.manage` |
| `SetNotificationRules` | `users.manage` and a recent second factor |

Web BFF serves `GET /api/v1/notifications`, `POST /api/v1/notifications/read`, `GET` and `PUT
/api/v1/notifications/rules`, and `GET /api/v1/audit/deliveries`.

## Check it on the cluster

```bash
make notification-check
```

## Run the tests

```bash
uv run pytest services/notification-service/tests
```

## Build the image

```bash
docker build -f services/notification-service/Dockerfile -t windops-notification:local .
```

## What the scaffold provides

- gRPC server on port 50051, using mutual TLS, with health, signed identity, deadlines, tracing and error mapping
- HTTP on port 8080: `/health/live`, `/health/ready` and `/metrics`
- JSON logs, OpenTelemetry export and graceful shutdown
- Kubernetes manifests in `deploy/`, for namespace `windops-application`

## Add behavior

1. Add the contract in `shared/protobuf`, and register its servicer in `register()` in
   `src/windops_notification/main.py`.
2. Add a readiness check for each dependency the service cannot work without.
3. Call other services through `ServiceClient`, with a breaker for each dependency.
4. Measure memory, and update the limits in `deploy/deployment.yaml`.
5. If the service stores data, add migrations with `make migration PACKAGE=windops_notification MESSAGE="..."`.
   [windops-migrations](../../shared/migrations/README.md) explains the rules.
