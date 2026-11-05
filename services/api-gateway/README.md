# windops-gateway

Apache APISIX is the only way into the cluster. It stores its configuration in `windops-etcd`.

It has no Python code, so it does not use the service template, but it follows the same rules:
it has its own manifests, probes and resource limits, and runs as a non-root user.

## Ports

| Port | Purpose | Reachable from |
| --- | --- | --- |
| 9080 | Proxy | The cluster, through the `windops-gateway` Service |
| 7085 | Status, for Kubernetes probes | The pod address, and not listed on the Service |
| 9180 | Admin API, with a key from a Secret | Inside the pod only |
| 9090 | Control API, including upstream health | Inside the pod only |

## Routes

Routes live in [infrastructure/gateway/routes](../../infrastructure/gateway/routes), one JSON file
each, and `make gateway-routes` applies them. Every upstream must have active health checks and
timeouts, so the gateway avoids an unhealthy backend and fails fast. A test enforces this.

Every route must also enable tracing. The plugin's gateway-wide settings are in
[infrastructure/gateway/plugin-metadata](../../infrastructure/gateway/plugin-metadata), and
`make gateway-routes` applies them too.

## Run it locally

```bash
make gateway          # deploy the gateway
make gateway-routes   # apply the routes
make gateway-forward  # reach it at http://127.0.0.1:9080
```

`make gateway-check` proves it on the real cluster, using a throwaway service.

## Policy

| Route | What it checks |
| --- | --- |
| `/api/*`, to Web BFF | The Keycloak token: RS256, the fixed issuer, the `windops-api` audience, and exactly one `tenant_id` (SEC-005). CORS for the frontend's origin only, and 600 requests a minute from one address |
| `/api/v1/public/*`, to Web BFF | No token, because sign-in pages need it first. CORS, and 60 requests a minute from one address |
| `/auth/realms/windops/login-actions/*`, to Keycloak | 5 sign-in attempts a minute from one address (SRS 7.5) |
| `/auth/realms/*`, to Keycloak | Only the sign-in pages. The admin API and the master realm are not routed |

Web BFF checks tokens and applies per-user limits again, so neither layer depends on the other. `make
gateway-policy-check` proves the policy on the cluster.

## Limits

- **Per-address limits share one address in local checks.** They count by the address the gateway sees, so
  checks arriving through a port-forward share `127.0.0.1`. Behind a cloud load balancer, the real client address
  must be read from its forwarded header, set up in Phase 15.
- **Limits are counted in each gateway pod.** They are kept in the pod's memory, so with two replicas a client could
  get up to twice its limit. That is fine for one replica.
- The root filesystem is writable, because APISIX generates its nginx configuration at startup.
- etcd has no authentication. M13.3 limits who can reach it with NetworkPolicies.
