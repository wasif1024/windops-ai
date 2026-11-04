# windops-sample-caller

TODO: state what this service owns, using its row in SRS section 7.2.

## Run the tests

```bash
uv run pytest services/sample-caller-service/tests
```

## Build the image

```bash
docker build -f services/sample-caller-service/Dockerfile -t windops-sample-caller:local .
```

## What the scaffold provides

- gRPC server on port 50051, with health, signed identity, deadlines, tracing and error mapping
- HTTP on port 8080: `/health/live`, `/health/ready` and `/metrics`
- JSON logs, OpenTelemetry export and graceful shutdown
- Kubernetes manifests in `deploy/`, for namespace `windops-application`

## Add behavior

1. Add the contract in `shared/protobuf`, and register its servicer in `register()` in
   `src/windops_sample_caller/main.py`.
2. Add a readiness check for each dependency the service cannot work without.
3. Call other services through `ServiceClient`, with a breaker for each dependency.
4. Measure memory, and update the limits in `deploy/deployment.yaml`.
