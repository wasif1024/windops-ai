# windops-audit

The authoritative record of business and security actions (SRS 7.2, FR-AUD-001). It owns the
`windops_audit` database.

## How it works

Services never write records directly. Each adds an `AuditRequested` event in the same transaction as its
own change, so a record cannot be lost or claimed without that change happening (EVT-004):

```python
await add_event(
    connection,
    audit_request_subject("alarm"),
    tenant_id=identity.tenant_id,
    payload=events_pb2.AuditRequested(action="alarm.acknowledged", actor_type="user", ...),
    producer="alarm",
)
```

| Rule | How it is enforced | Requirement |
| --- | --- | --- |
| A service cannot ask in another's name | It can publish only to `audit.requested.<its own name>`, and the producer must match | FR-AUD-001 |
| A repeated event creates one record | `event_id` is unique | Phase 2 exit gate, EVT-003 |
| Records cannot be changed | Database triggers refuse `UPDATE`, `DELETE` and `TRUNCATE`, for every login | FR-AUD-002 |
| Tampering is detectable | Each record's hash covers its content and the previous hash, per tenant | FR-AUD-002 |
| The chain cannot fork | One tenant's records cannot share a previous hash, enforced by the database | FR-AUD-002 |
| Agents are traceable | `actor_id` holds the agent, and `on_behalf_of` the user or service behind it | SEC-010 |
| A database outage loses nothing | Events wait in JetStream, and are never dead-lettered for it | SRS 7.7 |

Once stored, a record is announced with `audit.recorded`, through this service's own outbox.

## Read records

`ListAuditRecords` and `VerifyAuditChain` return only the caller's tenant, taken from its verified identity.

Both first ask the Authorization Service whether the caller has `audit.read`, which SRS 3.2 gives to Platform and
Tenant Administrators and Auditors. If Authorization cannot answer, nothing is read (SRS 7.7).

`ListAuditRecords` takes an optional `action`, such as `authorization.denied`, which the administrator console's
access denials use.

## Run the tests

```bash
uv run pytest services/audit-service/tests
```

## Build the image

```bash
docker build -f services/audit-service/Dockerfile -t windops-audit:local .
```

## Check it on the cluster

```bash
make audit-check
```

## Limits

- Anyone with the database's owner login, or an administrator, can turn the triggers off. The hash chain
  would show a change to a record, but not the removal of the newest ones. Anchoring the chain outside the
  database is not planned yet.
- Listing by `action` scans the tenant's records, as there is no index on it yet. That is fine at this size.
- Other services do not send audit requests yet. Each adds them as it is built.

## What the scaffold provides

- gRPC server on port 50051, with health, signed identity, deadlines, tracing and error mapping
- HTTP on port 8080: `/health/live`, `/health/ready` and `/metrics`
- JSON logs, OpenTelemetry export and graceful shutdown
- Kubernetes manifests in `deploy/`, for namespace `windops-application`

## Add behavior

1. Add the contract in `shared/protobuf`, and register its servicer in `register()` in
   `src/windops_audit/main.py`.
2. Add a readiness check for each dependency the service cannot work without.
3. Call other services through `ServiceClient`, with a breaker for each dependency.
4. Measure memory, and update the limits in `deploy/deployment.yaml`.
5. If the service stores data, add migrations with `make migration PACKAGE=windops_audit MESSAGE="..."`.
   [windops-migrations](../../shared/migrations/README.md) explains the rules.
