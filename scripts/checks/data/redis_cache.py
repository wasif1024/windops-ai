#!/usr/bin/env python3
"""Prove Redis and windops-cache work on the real local cluster, including a real outage.

Run `make secrets` and `make infra` first. This restarts Redis and scales it to zero for a moment,
which only empties the cache, and restores it afterwards.
"""

import asyncio
import subprocess
import sys

import redis.asyncio as redis
from redis.exceptions import AuthenticationError, NoPermissionError, ResponseError
from windops_cache import RateLimiter, RateLimitUnavailableError, TenantCache, connect
from windops_resilience import BreakerConfig, BreakerState, CircuitBreaker

from scripts.lib.tls_client import authority_file
from scripts.setup.gateway_routes import port_forward
from scripts.setup.provision_postgres import CONTEXT, database_name, read_passwords
from scripts.setup.redis_access import ADMIN_USER, NAMESPACE, SECRETS_FILE

DEPLOYMENT = "deployment/windops-redis"
SERVICE = "weather"
OTHER_SERVICE = "alarm"
TENANT = "redis-check"
OTHER_TENANT = "redis-check-other"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


def kubectl(*arguments: str) -> str:
    result = subprocess.run(
        ["kubectl", "--context", CONTEXT, f"--namespace={NAMESPACE}", *arguments],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"kubectl {arguments[0]} failed: {result.stderr}")
    return result.stdout


def wait_for_redis() -> None:
    kubectl("rollout", "status", DEPLOYMENT, "--timeout=120s")


def client_for(port: int, passwords: dict[str, str], service: str) -> "redis.Redis":
    return connect(
        host="127.0.0.1",
        port=port,
        username=database_name(service),
        password=passwords[service],
        ca_file=authority_file(),
    )


async def check_cache_rules(client: "redis.Redis", breaker: CircuitBreaker) -> None:
    cache = TenantCache(client, service=SERVICE, tenant_id=TENANT, breaker=breaker)
    other = TenantCache(client, service=SERVICE, tenant_id=OTHER_TENANT, breaker=breaker)
    check(await cache.set("value", b"kept", ttl_seconds=1), "a service can store an entry")
    check(await cache.get("value") == b"kept", "a service can read its own entry")
    check(await other.get("value") is None, "another tenant cannot read that entry (DAT-007)")
    await asyncio.sleep(1.5)
    check(await cache.get("value") is None, "the entry expires when its TTL ends")
    limiter = RateLimiter(client, service=SERVICE, tenant_id=TENANT, breaker=breaker)
    decisions = [await limiter.allow("check", limit=2, window_seconds=60) for _ in range(3)]
    check(
        [item.allowed for item in decisions] == [True, True, False],
        "a rate limit refuses the request over its limit",
    )


async def check_isolation(port: int, passwords: dict[str, str], client: "redis.Redis") -> None:
    denied = False
    try:
        await client.set(f"{OTHER_SERVICE}:{TENANT}:value", b"x", ex=5)
    except NoPermissionError:
        denied = True
    check(denied, "a service cannot write another service's keys (ARC-002)")
    denied = False
    try:
        await client.get(f"{OTHER_SERVICE}:{TENANT}:value")
    except NoPermissionError:
        denied = True
    check(denied, "a service cannot read another service's keys (ARC-002)")
    for name, call in (("FLUSHALL", client.flushall), ("KEYS", lambda: client.keys("*"))):
        denied = False
        try:
            await call()
        except NoPermissionError:
            denied = True
        check(denied, f"a service cannot run {name}")
    for username, password, label in (
        (None, None, "the default login is off"),
        (database_name(SERVICE), "wrong-password", "a wrong password is refused"),
    ):
        stranger = redis.Redis(
            host="127.0.0.1",
            port=port,
            username=username,
            password=password,
            ssl=True,
            ssl_ca_certs=authority_file(),
        )
        refused = False
        try:
            await stranger.get(f"{SERVICE}:{TENANT}:value")
        except (AuthenticationError, ResponseError):
            refused = True
        finally:
            await stranger.aclose()
        check(refused, label)


async def check_no_persistence(port: int, passwords: dict[str, str]) -> None:
    admin = connect(
        host="127.0.0.1",
        port=port,
        username=ADMIN_USER,
        password=passwords["admin"],
        ca_file=authority_file(),
    )
    settings = {
        **await admin.config_get("save"),
        **await admin.config_get("appendonly"),
        **await admin.config_get("maxmemory-policy"),
    }
    await admin.aclose()
    check(not settings["save"], "Redis takes no snapshots")
    check(settings["appendonly"] in ("no", b"no"), "Redis keeps no append-only file (EVT-005)")
    check(
        settings["maxmemory-policy"] in ("volatile-lru", b"volatile-lru"),
        "Redis removes only entries with a TTL when full",
    )


async def run_while_connected(port: int, passwords: dict[str, str]) -> None:
    client = client_for(port, passwords, SERVICE)
    breaker = CircuitBreaker("redis", BreakerConfig(slow_call_seconds=1.0, open_seconds=2.0))
    try:
        await check_cache_rules(client, breaker)
        await check_isolation(port, passwords, client)
        await check_no_persistence(port, passwords)
        cache = TenantCache(client, service=SERVICE, tenant_id=TENANT, breaker=breaker)
        await cache.set("restart", b"gone", ttl_seconds=300)
    finally:
        await client.aclose()


async def run_outage(passwords: dict[str, str]) -> None:
    """Take Redis away for real, then bring it back, with one breaker throughout."""
    breaker = CircuitBreaker("redis", BreakerConfig(slow_call_seconds=1.0, open_seconds=2.0))
    with port_forward("service/windops-redis", NAMESPACE, 6379) as port:
        client = client_for(port, passwords, SERVICE)
        cache = TenantCache(client, service=SERVICE, tenant_id=TENANT, breaker=breaker)
        limiter = RateLimiter(client, service=SERVICE, tenant_id=TENANT, breaker=breaker)
        check(await cache.get("restart") is None, "a restart empties the cache (EVT-005)")
        kubectl("scale", DEPLOYMENT, "--replicas=0")
        kubectl("wait", "pod", "--selector=app.kubernetes.io/name=windops-redis",
                "--for=delete", "--timeout=60s")  # fmt: skip
        check(await cache.get("anything") is None, "a read acts as a miss during an outage")
        check(await cache.set("x", b"x", ttl_seconds=5) is False, "a write reports it was skipped")
        refused = False
        try:
            await limiter.allow("outage", limit=100, window_seconds=60)
        except RateLimitUnavailableError:
            refused = True
        check(refused, "a rate limit refuses the request during an outage (SRS 7.7)")
        for _ in range(12):
            await cache.get("anything")
        check(breaker.state is BreakerState.OPEN, "the outage opens the circuit breaker")
        await client.aclose()
    kubectl("scale", DEPLOYMENT, "--replicas=1")
    wait_for_redis()
    with port_forward("service/windops-redis", NAMESPACE, 6379) as port:
        client = client_for(port, passwords, SERVICE)
        cache = TenantCache(client, service=SERVICE, tenant_id=TENANT, breaker=breaker)
        await asyncio.sleep(2.5)
        recovered = False
        for _ in range(5):
            if await cache.set("recovered", b"yes", ttl_seconds=5):
                recovered = True
        check(recovered, "the cache works again once Redis returns")
        check(breaker.state is BreakerState.CLOSED, "the circuit breaker closes again")
        await client.aclose()


def main() -> int:
    if not SECRETS_FILE.exists():
        print("No Redis passwords found. Run: make secrets", file=sys.stderr)
        return 1
    passwords = read_passwords(SECRETS_FILE)
    try:
        wait_for_redis()
        with port_forward("service/windops-redis", NAMESPACE, 6379) as port:
            asyncio.run(run_while_connected(port, passwords))
        kubectl("rollout", "restart", DEPLOYMENT)
        wait_for_redis()
        asyncio.run(run_outage(passwords))
    except (RuntimeError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        kubectl("scale", DEPLOYMENT, "--replicas=1")
    print("Redis check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
