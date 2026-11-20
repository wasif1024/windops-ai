---
name: maintenance-planning
description: How to prepare a wind turbine maintenance task for a person to decide on.
version: 2
---

# Maintenance planning

## A task

Prepare, for each proposed task:

- the action: inspection, repair, replacement, adjustment or monitoring;
- the steps, and who does them;
- whether it needs a climb, and the access it needs, onshore or offshore;
- the downtime, as a range;
- the parts and tools it is likely to need.

## Assumptions

- Name every assumption: the failure mode it treats, the parts, the crew, the weather.
- Parts: propose only parts that fit the turbine's model. A part is in stock only if `check_parts` said it was
  available just now; a short, incompatible, retired or unknown part is named as such, never assumed.
- A part you propose is a suggestion. Nothing is reserved until a person reserves it.

## What happens next

- A person decides. The Policy Service then checks the proposal against its rules, including weather and safety
  limits. A blocked or escalated check stands.
- Prefer the least invasive action the evidence supports, such as an inspection before a replacement.
