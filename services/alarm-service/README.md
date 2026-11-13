# windops-alarm

Alarms from each turbine's controller, and what operators do about them (SRS 4.3, FR-ALM-001 to 004).
It owns each alarm's state, its history and its severity. It never makes an alarm up: only a device
raises or clears one.

## How alarms arrive

A controller sends its alarms to the device route, `/ingest/v1/alarms`, as it sends samples
([ADR 0007](../../docs/adr/0007-alarms-arrive-through-the-device-route.md)). The Telemetry Service checks
each one and queues it as `alarm.reported`. This service consumes it:

| Report | What happens |
| --- | --- |
| `raise`, with nothing standing | A new alarm, `active`, from the device |
| `raise`, with the same condition standing | Its `repeat_count` rises. Nothing else changes |
| `clear`, with the condition standing | The alarm is `cleared`, whatever state it was in |
| `clear`, with nothing standing | Kept, and applied when the raise it belongs to arrives |
| The same report again | Nothing: each report ID is kept (EVT-003) |

A condition is a turbine, a code and a tag, such as `dogger-east-t01`, `GBX_OIL_TEMP_HIGH` and
`gearbox_oil_temp_c`. Once cleared, the same condition again is a new alarm, with its own history.

## States and who may change them

| From | To | By | Needs |
| --- | --- | --- | --- |
| `active` | `acknowledged` | An operator, with a comment if they like | `alarm.acknowledge` |
| `active` or `acknowledged` | `shelved`, for 1 to 1,440 minutes, with a reason | An operator | `alarm.acknowledge` |
| `shelved` | Where it was | An operator, or the system when its time is up | `alarm.acknowledge` |
| Any standing state | `cleared` | The device | |

Each change goes into the alarm's history with who made it and when (FR-ALM-002), and is announced as
`alarm.changed` (FR-ALM-003). An operator's action is also audited. Reading needs `alarm.read` for the
alarm's wind farm or turbine. The Asset Service, asked as the caller, says which wind farm a turbine is in.
An alarm the caller may not read is not found.

## Severity (FR-ALM-004)

Kept from the device, or, when it sends none, taken from the versioned catalogue in `catalog.py`. Each
alarm's `severity_source` says which: `device`, or `catalog:alarm-catalog-1`. A code the catalogue does
not know gets `medium`. Nothing changes an alarm's severity after it is made: not an operator, and never a
model.

## Run the tests

```bash
uv run pytest services/alarm-service/tests
make alarm-check   # on the cluster: a device's alarm, through the gateway, to an operator and back
```

## Build the image

```bash
docker build -f services/alarm-service/Dockerfile -t windops-alarm:local .
```
