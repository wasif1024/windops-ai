# Gateway routes

Add one JSON file per route. The file name is the route's ID. Web BFF adds the first real route
in M3.7.

```json
{
  "uri": "/api/*",
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

Every upstream must have `checks.active` and `timeout`, and `make check` fails without them.
Without timeouts, APISIX waits up to 60 seconds for a backend that is down.
