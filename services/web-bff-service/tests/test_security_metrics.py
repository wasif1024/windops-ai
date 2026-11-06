"""What Web BFF counts for the Security dashboard (SRS 10.3)."""

from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from test_web_bff import World, auth, token, with_bff
from windops_web_bff.api import PREFIX
from windops_web_bff.limits import LIMITS


def counted(reader: InMemoryMetricReader, name: str) -> dict[tuple[Any, ...], float]:
    data = reader.get_metrics_data()
    found: dict[tuple[Any, ...], float] = {}
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        if isinstance(point, NumberDataPoint):
                            labels = tuple(sorted((point.attributes or {}).items()))
                            found[labels] = point.value
    return found


def test_refused_tokens_are_counted_by_reason() -> None:
    reader = InMemoryMetricReader()

    async def scenario(world: World) -> None:
        await world.client.get(f"{PREFIX}/me")
        await world.client.get(f"{PREFIX}/me", headers=auth("not.a.token"))
        await world.client.get(f"{PREFIX}/me", headers=auth(token(tenant_id=None)))

    with_bff(scenario, services={"meter_provider": MeterProvider(metric_readers=[reader])})
    assert counted(reader, "windops.web_bff.tokens_refused") == {
        (("reason", "missing"),): 1,
        (("reason", "invalid"),): 1,
        (("reason", "no_tenant"),): 1,
        # Started at 0, so Prometheus sees the first one as a change.
        (("reason", "keys_unavailable"),): 0,
    }


def test_requests_over_the_limit_are_counted_without_naming_the_user() -> None:
    reader = InMemoryMetricReader()

    async def scenario(world: World) -> None:
        bearer = auth(token())
        for _ in range(LIMITS["standard"] + 2):
            await world.client.get(f"{PREFIX}/me", headers=bearer)

    with_bff(scenario, services={"meter_provider": MeterProvider(metric_readers=[reader])})
    assert counted(reader, "windops.web_bff.rate_limited") == {
        (("traffic_class", "standard"),): 2,
        (("traffic_class", "administrator"),): 0,
    }
