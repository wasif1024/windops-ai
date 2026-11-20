# ADR 0030: Spare parts checked by anyone, stock changed or reserved only by a person

**Status:** Accepted, 2026-11-20, for M9.2.

## Context

SRS 4.8 gives the Inventory Service three requirements:
- **FR-INVT-001:** expose parts, compatibility, stock and reservation status.
- **FR-INVT-002:** treat an agent's part recommendation as a suggestion, not a transaction.
- **FR-INVT-003:** require an authorized user, or a separately authorized deterministic workflow, for stock updates
  and reservations.

The permissions already exist (SRS 3.2):
- `inventory.read`: Operations Manager, Reliability Engineer, Maintenance Planner, Maintenance Manager and Auditor.
- `inventory.update` and `inventory.reserve`: the Maintenance Planner.

The Authorization Service holds every identity with an agent behind it to reads and `maintenance.draft` (ADR 0019).
The service map says Inventory calls no other domain service, and announces `evidence.changed` (SRS 8.5: a proposed
part that is no longer available makes a recommendation stale). ADR 0029 left a work order's part checks to this
module.

## Decision

**The Inventory Service owns four records:**
- **Parts:** the tenant's catalog. Each part has a number, a name, a component kind and a unit, and the turbine
  models it fits by manufacturer and model. A consumable may fit any model instead. A retired part is kept, but is
  never available or reserved again.
- **Stock:** each part in each wind farm's store. It is on hand and reserved, and available is the difference. A
  database constraint keeps on hand from falling below reserved.
- **Reservations:** parts held in a store for a work order, `held` then `released`. Every line is held, or none.
- **The stock ledger:** every change to a store's stock, as it happened. A trigger keeps it unchanged.

**Stores are wind farms.** A store's stock is scoped like a turbine's records: `inventory.read`, `inventory.update`
or `inventory.reserve` for its wind farm, or for the tenant. The catalog is the tenant's reference data, readable
with `inventory.read` in any scope, and changed with `inventory.update` for the tenant.
- **A store is not checked against the Asset Service,** since Inventory calls no domain service. The permission
  scope still names the wind farm, so a grant for one farm covers only its store.

**Compatibility is by turbine model.** A part fits the models it names, matched without regard to case or spacing,
or any model.
- **`CheckParts`:** for a model and a wind farm, gives each part as `available`, `short`, `incompatible`, `retired`
  or `unknown`, with what the store has available. It holds nothing.
- **A component kind:** each part has one, so a planner can filter by it. It is not checked against a turbine's
  own components, which are not yet kept per model.

**Only a person changes stock or reserves it (FR-INVT-002, FR-INVT-003).** The Inventory Service refuses any
identity with an agent behind it, or with no person, before it asks Authorization, so no change to the permission
table could let an agent reserve. It refuses these, as well as reservations:
- adding or changing a part;
- adjusting stock;
- releasing a reservation.

**A deterministic workflow** may reserve once one exists, such as reserving a work order's parts when it is
approved in Phase 10. It will need its own fixed grant, and a change to this rule, recorded then.

**Agents read parts, and suggest them:**
- **WindOps MCP's `find_parts`:** lists the parts that fit a turbine's model, with its wind farm's available stock.
- **WindOps MCP's `check_parts`:** checks named parts against the turbine and its store.
- **Where the turbine's details come from:** both take the turbine's model and wind farm from the Asset Service,
  never from the agent.
- **Who holds them:** only the Maintenance Planner.
- **A suggestion:** a part an agent proposes goes into a draft's plan. Nothing is held until a person reserves it.

**A work order's parts are checked as it is validated.** The Maintenance Service asks `CheckParts` as the caller,
with the turbine's model and wind farm. The result is kept in the validation, part by part.

| Found | Effect |
| --- | --- |
| `unknown`, `retired` or `incompatible` | Blocks submission |
| `short` | Noted for the reviewer, as the parts may be ordered or moved before the window |
| The Inventory Service cannot be asked | Blocks, and no part is shown as available (SRS 7.7) |
| A part named twice in a plan | A blocking problem of the plan's own, so Inventory is not asked |

**Recorded:**
- **Audit:** every change is audited as the person who made it, in the same transaction.
- **Announced:** every change is announced as `evidence.changed` through the outbox. A part's record is
  `part:<number>`, and a store's stock `stock:<number>@<wind farm>`, each with its new version.

## Consequences

- **Proved by `make inventory-check`, with the real services:**
  - **A planner's parts:**
    - two parts are added and one stocked;
    - checked against the demo turbine's model, they are short, incompatible and unknown;
    - one is reserved and then released, and the store follows;
    - each change is kept in the ledger, which cannot be changed, then audited and announced.
  - **An operator** may not read inventory.
  - **Another tenant** cannot find the part.
  - **The Maintenance Planner agent** finds and checks the part through WindOps MCP, with its own token.
  - **Every identity with an agent behind it, or with no person,** is refused when it tries to reserve or adjust
    stock.
  - **A work order's validation** names its part available, and blocks one that does not fit.
- **`make maintenance-check`** passes unchanged. A plan without parts does not ask Inventory.
- **Not yet:**
  - **The planner's own use of these tools,** in its proposal and step 12's draft, comes with M9.3.
  - **The parts lookup screen:** M9.4.
  - **Using `evidence.changed` to mark a recommendation stale:** Phase 10.
  - **Consuming reserved parts** when work is completed: Phase 10.
  - **Parts prices** for the Production Impact Service: no unit cost is kept yet.
