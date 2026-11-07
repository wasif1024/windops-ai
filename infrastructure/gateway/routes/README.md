# Gateway routes

Add one JSON file per route. The file name is the route's ID. `web-bff.json` shows the full policy: token
checks, CORS and a per-address limit.

```json
{
  "uri": "/api/*",
  "plugins": {"opentelemetry": {"sampler": {"name": "always_on"}}},
  "upstream": {
    "type": "roundrobin",
    "timeout": {"connect": 2, "send": 10, "read": 10},
    "nodes": {"windops-web-bff.windops-system.svc.cluster.local:8080": 1},
    "checks": {
      "active": {
        "type": "http",
        "http_path": "/health/ready",
        "healthy": {"interval": 2, "successes": 2},
        "unhealthy": {"interval": 2, "http_failures": 2, "tcp_failures": 2, "timeouts": 2}
      }
    }
  }
}
```

Every route must have the `opentelemetry` plugin, and every upstream must have `checks.active` and
`timeout`. `make check` fails without them.
Without timeouts, APISIX waits up to 60 seconds for a backend that is down.

`ingest.json` is the device route (API-004). It has no user sign-in and no CORS, as devices are not browsers: the
Telemetry Service checks each device's own credential. Its per-address limit is coarse, high enough for a whole fleet
behind one address. Each device's real limit, 600 requests a minute, is counted by the service.
