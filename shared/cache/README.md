# windops-cache

Redis access for services, built so the SRS 8.1 rules cannot be forgotten.

| Rule | How it is enforced | Requirement |
| --- | --- | --- |
| Every entry expires | `set` needs a TTL, of at most 24 hours | SRS 8.1 |
| Keys carry a tenant | Every key is `<service>:<tenant>:<name>`, and the tenant comes from the verified identity | DAT-007 |
| Credentials are not cached | Names that look like tokens, passwords, secrets or sessions are refused | DAT-004 |
| One service cannot touch another's keys | The Redis login allows only its own `<service>:` prefix | ARC-002 |
| An outage does not spread | Calls go through a circuit breaker | SRS 7.7 |

Redis is not a system of record. Anything that must survive a restart belongs in PostgreSQL or JetStream
(EVT-005).

## Use it

```python
from windops_cache import RateLimiter, TenantCache, cache_breaker, connect

client = connect(host=host, port=port, username=username, password=password)
breaker = cache_breaker()

cache = TenantCache(client, service="weather", tenant_id=identity.tenant_id, breaker=breaker)
await cache.set("forecast:farm-7", payload, ttl_seconds=300)
payload = await cache.get("forecast:farm-7")  # None on a miss, or when Redis is down
```

## When Redis is down

| Call | Result | What the caller does |
| --- | --- | --- |
| `TenantCache.get` | `None`, like a miss | Reads from the source service, more slowly |
| `TenantCache.set` or `delete` | `False` | Continues. A `delete` that fails leaves the old value until its TTL ends, so choose short TTLs for data that changes |
| `RateLimiter.allow` | Raises `RateLimitUnavailableError` | Refuses the request on a protected route |
