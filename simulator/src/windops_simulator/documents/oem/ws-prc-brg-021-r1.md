# Main Bearing: Inspection, Regreasing and Wear Assessment

| Field | Value |
| --- | --- |
| Document | WS-PRC-BRG-021 |
| Revision | 1 |
| Type | procedure |
| Published | 2025-05-30 |
| Issued by | WindOps Synthetic (fictional manufacturer) |
| Applies to | WS-8.0-167, WS-4.5-145 |
| Classification | internal |

> **Synthetic document.** Written for testing WindOps AI, about a fictional turbine from a fictional manufacturer. It
> describes no real product, and must not be used on real equipment.

## 1. When to use this procedure

Use it for scheduled regreasing, when `MAIN_BEARING_TEMP_HIGH` is raised, or when the CMS main bearing sensor shows
the signs of wear in section 2.

## 2. Signs of wear

A worn main bearing heats and rings. Wear usually grows slowly at first, then faster, over days to weeks.

| Sign | Where to look |
| --- | --- |
| Temperature above its history at the same load and air temperature, climbing over hours | The main bearing temperature |
| Impulsiveness: kurtosis of the main bearing window above 4, a healthy one being about 3 | CMS main bearing sensor |
| Energy at the outer-race defect frequency, 8.1 orders of the rotor | CMS main bearing sensor |
| Iron and chromium in the purged grease | The laboratory's grease report |

The main shaft turns slowly, so its defect frequency is low: at 10 rpm, 8.1 orders is about 1.35 Hz. Each impact
rings the structure at its resonance, so wear shows first as impulsiveness, before a clear defect tone.

Temperature alone does not prove wear: too much grease also runs hot, for the first day after regreasing. Look for
the CMS signs too.

## 3. Regreasing

The turbine is stopped, isolated and locked, and the rotor locked (WS-SAF-001).

1. Open the purge outlets.
2. Pump the grease quantity on the nacelle's lubrication plate into each inlet, slowly, by hand.
3. Run the rotor slowly, if the site's procedure allows, to spread it.
4. Collect a sample of the purged grease from each outlet for the laboratory.
5. Close the outlets once purged grease has stopped flowing.

## 4. Wear assessment

| Finding | Action |
| --- | --- |
| Temperature normal, CMS normal, grease clean | No action |
| Temperature high, CMS normal | Check grease quantity and the last regreasing; watch for a week |
| CMS signs of wear, grease with wear metals | Plan a borescope inspection; shorten the regreasing interval by half; plan replacement if wear grows |
| Temperature climbing quickly with strong CMS signs | Stop the turbine; a person decides on restart |

A main bearing replacement needs a large crane or a jack-up vessel offshore, and several days. Plan it with the
asset's production impact.
