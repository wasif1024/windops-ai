"""Record the synthetic demo assets for the demo tenants. Safe to repeat.

Run inside the service's pod, with its own database login, by `make asset-seed`:

    python -m windops_asset.seed

Everything here is invented, and says so: the manufacturer is "WindOps Synthetic" (SIM-006).
The IDs are fixed, so a second run finds each record and changes nothing.
"""

import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncConnection
from windops_migrations import create_service_engine, in_tenant

from windops_asset.store import COMPONENTS, TURBINES, WIND_FARMS, record_change

ACTOR = "seed"
MANUFACTURER = "WindOps Synthetic"


@dataclass(frozen=True)
class Farm:
    tenant_id: str
    wind_farm_id: str
    name: str
    country: str
    latitude: float
    longitude: float
    time_zone: str
    offshore: bool
    turbines: int


@dataclass(frozen=True)
class Model:
    model: str
    rated_power_kw: float
    rotor_diameter_m: float
    hub_height_m: float


FARMS = (
    Farm("north-sea", "dogger-east", "Dogger East", "GB", 54.82, 2.07, "Europe/London", True, 4),
    Farm("north-sea", "hornsea-west", "Hornsea West", "GB", 53.91, 1.62, "Europe/London", True, 4),
    Farm("atlas-wind", "atlas-ridge", "Atlas Ridge", "ES", 42.13, -3.71, "Europe/Madrid", False, 5),
)
OFFSHORE = Model("SG-8.0-167", 8000, 167, 109)
ONSHORE = Model("SG-4.5-145", 4500, 145, 105)
# (slug, kind, name, parent slug). Parents come first, so a part's parent always exists.
PARTS = (
    ("tower", "tower", "Tower", ""),
    ("nacelle", "nacelle", "Nacelle", ""),
    ("rotor", "rotor", "Rotor", "nacelle"),
    ("hub", "hub", "Hub", "rotor"),
    ("blade-a", "blade", "Blade A", "hub"),
    ("blade-b", "blade", "Blade B", "hub"),
    ("blade-c", "blade", "Blade C", "hub"),
    ("pitch", "pitch_system", "Pitch system", "hub"),
    ("main-shaft", "main_shaft", "Main shaft", "nacelle"),
    ("main-bearing", "main_bearing", "Main bearing", "nacelle"),
    ("gearbox", "gearbox", "Gearbox", "nacelle"),
    ("gearbox-hss-bearing", "gearbox_bearing", "High-speed shaft bearing", "gearbox"),
    ("gearbox-ims-bearing", "gearbox_bearing", "Intermediate shaft bearing", "gearbox"),
    ("gearbox-oil", "gearbox_oil_system", "Gearbox oil system", "gearbox"),
    ("generator", "generator", "Generator", "nacelle"),
    ("generator-bearing", "generator_bearing", "Generator bearing", "generator"),
    ("converter", "converter", "Converter", "nacelle"),
    ("transformer", "transformer", "Transformer", "tower"),
    ("yaw", "yaw_system", "Yaw system", "nacelle"),
    ("cooling", "cooling_system", "Cooling system", "nacelle"),
    ("controller", "controller", "Turbine controller", "nacelle"),
)


async def _exists(connection: AsyncConnection, query: Select[tuple[str]]) -> bool:
    return (await connection.execute(query)).first() is not None


async def seed_farm(connection: AsyncConnection, farm: Farm, now: datetime) -> int:
    """Add what is missing of one wind farm, and return how many records were added."""
    added = 0
    tenant = farm.tenant_id
    await in_tenant(connection, tenant)
    found = select(WIND_FARMS.c.wind_farm_id).where(
        WIND_FARMS.c.tenant_id == tenant, WIND_FARMS.c.wind_farm_id == farm.wind_farm_id
    )
    if not await _exists(connection, found):
        await connection.execute(
            WIND_FARMS.insert().values(
                tenant_id=tenant,
                wind_farm_id=farm.wind_farm_id,
                name=farm.name,
                country=farm.country,
                latitude=farm.latitude,
                longitude=farm.longitude,
                time_zone=farm.time_zone,
                offshore=farm.offshore,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        await record_change(
            connection,
            tenant_id=tenant,
            actor_id=ACTOR,
            actor_type="service",
            action="asset.wind_farm.created",
            resource_type="wind_farm",
            resource_id=farm.wind_farm_id,
        )
        added += 1
    model = OFFSHORE if farm.offshore else ONSHORE
    for number in range(1, farm.turbines + 1):
        turbine_id = f"{farm.wind_farm_id}-t{number:02d}"
        found = select(TURBINES.c.turbine_id).where(
            TURBINES.c.tenant_id == tenant, TURBINES.c.turbine_id == turbine_id
        )
        if await _exists(connection, found):
            continue
        # Spread along a row about 1 km apart, as a real array is.
        row = (
            await connection.execute(
                TURBINES.insert()
                .values(
                    tenant_id=tenant,
                    turbine_id=turbine_id,
                    wind_farm_id=farm.wind_farm_id,
                    name=f"{farm.name} T{number:02d}",
                    latitude=round(farm.latitude + 0.009 * (number - 1), 5),
                    longitude=farm.longitude,
                    manufacturer=MANUFACTURER,
                    model=model.model,
                    rated_power_kw=model.rated_power_kw,
                    rotor_diameter_m=model.rotor_diameter_m,
                    hub_height_m=model.hub_height_m,
                    serial_number=f"SYN-{turbine_id.upper()}",
                    commissioned_on="2024-05-17",
                    status="operating",
                    status_reason="",
                    status_changed_at=now,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                .returning(*TURBINES.c)
            )
        ).one()
        await connection.execute(
            COMPONENTS.insert(),
            [
                {
                    "tenant_id": tenant,
                    "component_id": f"{turbine_id}-{slug}",
                    "turbine_id": turbine_id,
                    "parent_component_id": f"{turbine_id}-{parent}" if parent else "",
                    "kind": kind,
                    "name": name,
                    "manufacturer": MANUFACTURER,
                    "model": "",
                    "serial_number": f"SYN-{turbine_id.upper()}-{slug.upper()}",
                    "installed_on": "2024-05-17",
                    "status": "in_service",
                    "status_reason": "",
                    "version": 1,
                    "updated_at": now,
                }
                for slug, kind, name, parent in PARTS
            ],
        )
        await record_change(
            connection,
            tenant_id=tenant,
            actor_id=ACTOR,
            actor_type="service",
            action="asset.turbine.created",
            resource_type="turbine",
            resource_id=turbine_id,
            turbine=row,
        )
        added += 1 + len(PARTS)
    return added


async def seed() -> int:
    engine = create_service_engine()
    added = 0
    try:
        for farm in FARMS:
            # One transaction per wind farm, so each is all there or not at all.
            async with engine.begin() as connection:
                added += await seed_farm(connection, farm, datetime.now(UTC))
    finally:
        await engine.dispose()
    return added


def main() -> int:
    added = asyncio.run(seed())
    print(f"Added {added} records" if added else "Already seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
