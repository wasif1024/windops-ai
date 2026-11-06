# Service certificates

Every service proves who it is with its own certificate, and every gRPC call uses mutual TLS (GRP-005). The
certificates come from [cert-manager](https://cert-manager.io), which K8S-002 allows, so no service mesh is needed.

```text
windops-selfsigned (issuer) → windops-internal-ca → windops-internal (issuer) → one certificate per service
```

## What a service gets

`make service` adds `deploy/certificate.yaml`, which gives each service:

| Item | Value |
| --- | --- |
| Secret | `windops-<service>-tls`, mounted at `/var/run/windops/tls` |
| Identity | The certificate's common name, such as `windops-audit`, which the server reads from each call |
| Address | The service's in-cluster DNS names, which a client checks |
| Lifetime | 30 days, renewed 7 days before it ends, with a new key each time |

## How a call is protected

| Check | Who does it | What it stops |
| --- | --- | --- |
| The server's certificate matches the address | The client | Connecting to the wrong service, or an impostor |
| The client has a certificate from this CA | The server | Anything else in the cluster calling a service |
| The call carries a signed identity and a deadline | The server | A call with no user or tenant (GRP-003, GRP-004) |

A service refuses to start without its certificate. Only the `test` environment can turn this off, using
`WINDOPS_PLAINTEXT_GRPC=true`, and only for checks that run a container outside the cluster.

## Renewal

A service watches its certificate files, and exits cleanly when they change, so Kubernetes restarts it with the new
ones. Its clients' breakers cover the moment it is away. This happens about every 23 days per service.

## Data stores

PostgreSQL, Redis, Qdrant and NATS each have a certificate from the same CA, and accept only TLS.

| Item | How it works |
| --- | --- |
| Encryption | A plain connection is refused, even with the right password |
| The client checks the store | Against the CA and the store's name, so nothing can pose as it |
| The store checks the client | With its existing login, so it is TLS and a password, not mutual TLS |
| Renewal | PostgreSQL, Redis and NATS load the new certificate within a minute, with no restart. Qdrant restarts cleanly, because its gRPC port cannot reload one |

Migration Jobs and Keycloak are not gRPC services, so they mount only the CA, from the `windops-trust` Secret.
`make data-tls-check` proves all of this on the cluster.

## From your machine

Checks reach a service using `scripts/lib/tls_client.py`, which requests a client certificate named
`windops-check-client`.

## Limits

- All services share one signing key for internal identity tokens, so a compromised service could forge a user's
  identity for its calls. mTLS proves which service is calling, but services do not yet refuse callers that should
  not reach them, or tokens that do not match the caller.
- Data stores check a password, not a client certificate, so a stolen password still works from inside the
  cluster. M13.3's network policies limit where it can be used from.
- **Plain HTTP for probes and metrics only.** The API Gateway reaches Web BFF and Keycloak over HTTPS, and checks
  their certificates. Prometheus scrapes `/metrics`, and Kubernetes probes reach `/health/*`, over plain HTTP,
  because those hold no business data.
- The CA renews once a year and keeps its key, so services never disagree about whom to trust. That also means
  replacing a leaked CA key is a manual job, which restarts every service.
- Nothing warns yet if cert-manager stops renewing certificates. Prometheus does not scrape it, so a service's
  certificate could expire without an alert. The check is `kubectl get certificates --all-namespaces`.
- The CA's key is a Kubernetes Secret, so anyone who can read Secrets in `cert-manager` can issue certificates. A
  production deployment would keep it in a managed key store (Phase 15).
