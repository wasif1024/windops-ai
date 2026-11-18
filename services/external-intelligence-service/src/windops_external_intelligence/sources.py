"""What the service needs to know of the tenant, asked as itself (ADR 0008).

From the Asset Service, with its fixed grant of turbine.read: the tenant's wind farms and
turbines, for the names to take out of a query, a site's region and a turbine's class. From the
Tenant Service, which anyone in a tenant may read: the tenant's own name. Kept for five minutes
per tenant, so a search does not ask for the whole fleet each time.
"""

import time
from dataclasses import dataclass
from typing import Any, Protocol

import grpc
from google.protobuf.message import Message
from windops.asset.v1 import asset_pb2
from windops.tenant.v1 import tenant_pb2
from windops_grpc import NotFoundError, ServiceClient, UnavailableError
from windops_resilience import BreakerOpenError

from windops_external_intelligence import outbound

DEADLINE_SECONDS = 3.0
KEEP_SECONDS = 300.0


async def ask(client: ServiceClient, method: Any, request: Message, who: str) -> Any:
    try:
        return await client.call(
            method, request, idempotent=True, deadline_seconds=DEADLINE_SECONDS
        )
    except grpc.aio.AioRpcError as error:
        if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED):
            raise NotFoundError(f"{who} has no such record") from error
        raise UnavailableError(f"{who} cannot be reached right now") from error
    except (BreakerOpenError, TimeoutError) as error:
        raise UnavailableError(f"{who} cannot be reached right now") from error


@dataclass(frozen=True)
class Fleet:
    identifiers: outbound.Identifiers
    # By wind farm ID, and by turbine ID: what a region and a class are made from.
    sites: dict[str, outbound.Site]
    turbines: dict[str, tuple[str, float]]


class Sources(Protocol):
    async def fleet(self, tenant: str) -> Fleet: ...


class ServiceSources:
    def __init__(
        self, asset: Any, tenant: Any, asset_client: ServiceClient, tenant_client: ServiceClient
    ) -> None:
        self._asset, self._tenant = asset, tenant
        self._asset_client, self._tenant_client = asset_client, tenant_client
        self._kept: dict[str, tuple[float, Fleet]] = {}

    async def fleet(self, tenant: str) -> Fleet:
        kept = self._kept.get(tenant)
        if kept is not None and time.monotonic() - kept[0] < KEEP_SECONDS:
            return kept[1]
        farms = (
            await ask(
                self._asset_client,
                self._asset.ListWindFarms,
                asset_pb2.ListWindFarmsRequest(),
                "The Asset Service",
            )
        ).wind_farms
        turbines: list[Any] = []
        token = ""
        while True:
            page = await ask(
                self._asset_client,
                self._asset.ListTurbines,
                asset_pb2.ListTurbinesRequest(page_size=500, page_token=token),
                "The Asset Service",
            )
            turbines.extend(page.turbines)
            token = page.next_page_token
            if not token:
                break
        try:
            record = (
                await ask(
                    self._tenant_client,
                    self._tenant.GetTenant,
                    tenant_pb2.GetTenantRequest(),
                    "The Tenant Service",
                )
            ).tenant
            names: tuple[str, ...] = (tenant, record.name)
        except NotFoundError:
            names = (tenant,)
        sites = {
            f.wind_farm_id: outbound.Site(
                f.country, f.location.latitude, f.location.longitude, f.offshore
            )
            for f in farms
        }
        found = Fleet(
            identifiers=outbound.Identifiers(
                tenant=names,
                sites=tuple(v for f in farms for v in (f.wind_farm_id, f.name)),
                turbines=tuple(
                    v
                    for t in turbines
                    for v in (t.turbine_id, t.name, t.serial_number, t.model.model)
                ),
            ),
            sites=sites,
            turbines={t.turbine_id: (t.wind_farm_id, t.model.rated_power_kw) for t in turbines},
        )
        self._kept[tenant] = (time.monotonic(), found)
        return found
