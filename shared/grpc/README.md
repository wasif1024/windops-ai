# windops-grpc

The gRPC server, client and runtime every WindOps service shares. Create services with
`make service NAME=telemetry-service`, which uses this package.

## Incoming calls

`ServerContextInterceptor` runs on every call except the gRPC health service, and:

- Rejects a missing, invalid or expired identity with `UNAUTHENTICATED` before the handler runs.
- Rejects a call with no deadline with `INVALID_ARGUMENT` (GRP-003).
- Continues the caller's trace, and adds the request ID and tenant to logs.
- Makes the verified identity available through `current_identity()` (GRP-004).

Raise a `DomainError` subclass for expected failures. Its message goes to the caller. Any other
exception returns `INTERNAL` with a generic message, and its details stay in the logs (GRP-006).

| Error | gRPC status |
| --- | --- |
| `InvalidRequestError` | `INVALID_ARGUMENT` |
| `NotFoundError` | `NOT_FOUND` |
| `PermissionDeniedError` | `PERMISSION_DENIED` |
| `ConflictError` | `FAILED_PRECONDITION` |
| `UnavailableError` | `UNAVAILABLE` |

## Outgoing calls

```python
telemetry = ServiceClient(CircuitBreaker("telemetry", BreakerConfig(slow_call_seconds=2.0)))

snapshot = await telemetry.call(
    stub.CreateTelemetrySnapshot,
    request,
    idempotent=True,  # The request carries an idempotency key.
    deadline_seconds=5.0,
)
```

`ServiceClient` adds the deadline, trace context and request ID, and forwards the current
identity. An automated run passes its own token through `identity_token`.

## Limits

- Calls use mutual TLS, from `WINDOPS_TLS_DIR`. [Service certificates](../../infrastructure/security/pki/README.md)
  explains how. Open a channel with `create_channel`, which uses the service's certificate.
- Identity tokens use one shared signing key, read from `WINDOPS_IDENTITY_SECRET`, so a compromised service could
  forge a user's identity.
- `ServiceClient` makes unary calls only. Streaming clients arrive with M4.7.
