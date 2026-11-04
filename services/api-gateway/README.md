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

## Run it locally

```bash
make gateway          # deploy the gateway
make gateway-routes   # apply the routes
make gateway-forward  # reach it at http://127.0.0.1:9080
```

`make gateway-check` proves it on the real cluster, using a throwaway service.

## Limits

- It serves plain HTTP. M3.7 adds TLS, CORS and rate limits, and M3.1 adds OIDC checks.
- The root filesystem is writable, because APISIX generates its nginx configuration at startup.
- etcd has no authentication. M13.3 limits who can reach it with NetworkPolicies.
