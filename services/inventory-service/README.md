# windops-inventory

Spare parts: the catalog, which turbine models each part fits, the stock in each wind farm's store, and reservations
against work orders (SRS 4.8, FR-INVT-001 to FR-INVT-003). Only a person changes stock or reserves it; an agent's
choice of part is a suggestion ([ADR 0030](../../docs/adr/0030-parts-checked-by-anyone-reserved-by-a-person.md)).

## What it keeps

| Record | What it holds |
| --- | --- |
| A part | Its number, name, description, component kind and unit; the turbine models it fits, or that it fits any model; whether it is retired; a version |
| A store's stock | One part in one wind farm's store: on hand, reserved, and available, which is on hand less reserved. Never less on hand than reserved |
| A reservation | Parts held in a store for a work order: `held`, then `released`. `consumed` comes with Phase 10 |
| The stock ledger | Every change to a store's stock, as it happened. A database trigger keeps it unchanged |

## Who may do what

| Call | Needs |
| --- | --- |
| `GetPart`, `ListParts` | `inventory.read` in any scope. With a store named, `inventory.read` for its wind farm |
| `GetStock` | `inventory.read`: only the stores the caller may read are returned |
| `CheckParts` | `inventory.read` for the wind farm |
| `ListReservations` | `inventory.read` for the named wind farm, or for the whole tenant |
| `PutPart` | `inventory.update` for the tenant, and a person |
| `AdjustStock` | `inventory.update` for the wind farm, and a person |
| `ReserveParts`, `ReleaseReservation` | `inventory.reserve` for the wind farm, and a person |

**A person, never an agent (FR-INVT-002, FR-INVT-003, SEC-009).** This service refuses any identity with an agent
behind it, or with no person, before it changes or reserves anything, whatever the Authorization Service would allow.
A separately authorized deterministic workflow may reserve once Phase 10 adds one. Each refusal is audited as
`authorization.denied`, with the agent as the actor on its principal's behalf, so the Denials page shows it.

## Checking parts

`CheckParts` says, for a turbine's model and wind farm, whether each part is `available`, `short`, `incompatible`,
`retired` or `unknown`. A model is matched without regard to case or spacing. The Maintenance Service asks it each
time it validates a work order, and WindOps MCP's `find_parts` and `check_parts` give it to the Maintenance Planner
agent. Checking holds nothing.

## Events

Each change is audited, and announced as `evidence.changed`, with `source_service` `inventory`:

| `record_id` | When |
| --- | --- |
| `part:<part number>` | A part is added or changed, retiring it included |
| `stock:<part number>@<wind farm>` | A store's stock changes: adjusted, reserved or released. `version` is the stock's |

## Run the tests

```bash
uv run pytest services/inventory-service/tests
make inventory-check   # on the cluster: parts, stock, reservations, and agents held to suggestions
```
