# gRPC contracts

Every synchronous service-to-service call uses gRPC and a versioned Protobuf contract
(GRP-001). An exception needs a [decision record](adr/). Contracts live in
`shared/protobuf`, and `make contracts` checks them.

## Layout and versions

- Each package is `windops.<domain>.v<major>`, in a matching directory (GRP-002), such as
  `shared/protobuf/windops/telemetry/v1/telemetry.proto`.
- Buf's `STANDARD` lint rules apply, with one exception: calls may return domain messages
  such as `TelemetrySnapshot`, as SRS 7.4 does, rather than a `...Response` wrapper.

## Changing a contract

Run `make contracts` first. It compares your contracts with `origin/main`, so run
`git fetch origin main` if the comparison is out of date.

| Change | Allowed in the same version |
| --- | --- |
| Add a field, message, enum value or call | Yes |
| Remove or rename a field, or change its number or type | No |
| Remove a message, enum value or call | No |

A change that breaks a contract goes in a new version, such as `windops.telemetry.v2`,
and the old version keeps working until nothing calls it.

## Rules for every call

| Rule | Requirement | Checked by |
| --- | --- | --- |
| State-changing calls accept an `idempotency_key` | GRP-007 | `tests/test_contracts.py` |
| Tenant and user context comes from verified metadata, never request fields | GRP-004 | `tests/test_contracts.py` for `tenant_id` and `user_id` in requests |
| Large results use pagination or server streaming | GRP-008 | Review |
| Calls carry deadlines, cancellation, trace context, request ID and signed identity | GRP-003 | The service scaffold, from M1.6 |
| Services implement the gRPC health protocol and consistent error mapping | GRP-006 | The service scaffold, from M1.6 |
| Calls use mTLS and service identities | GRP-005 | The service scaffold, `make mtls-check` and `make data-tls-check` |
