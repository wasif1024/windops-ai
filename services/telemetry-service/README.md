# windops-telemetry

Takes in each turbine's operational samples, as its device sends them, and keeps them by tenant and turbine
(FR-TEL-001, FR-TEL-002). Each device has its own credential, for one tenant and one turbine (FR-TEL-005). Fault
detection, data-quality labels, summaries and the fleet screens all read from it, as later modules add them.

```mermaid
flowchart LR
    Device["Device (the simulator)"] -->|"HTTPS, device token"| Gateway["API Gateway: /ingest/*"]
    Gateway -->|"HTTPS, certificate checked"| Telemetry["Telemetry Service"]
    BFF["Web BFF"] -->|"gRPC, mTLS, signed identity"| Telemetry
    Telemetry -->|"GetTurbine, Check"| Others["Asset and Authorization"]
    Telemetry -->|"per-device limit"| Redis[("Redis")]
    Telemetry -->|"row-level security"| DB[("PostgreSQL: windops_telemetry")]
    Telemetry -->|"outbox: audit"| NATS[("JetStream")]
```

## The device route (API-004)

`POST /ingest/v1/samples`, through the gateway, with `Authorization: Bearer <tenant>.<device>.<secret>` and a body of
`{"samples": [...]}`. Each sample is the simulator's schema version `2`; see
[windops-simulator](../../simulator/README.md#each-sample). The answer says how many were stored, and why each other
one was refused, by its place in the batch:

```json
{"accepted": 58, "rejected": [{"index": 12, "reason": "bad_event_time"}, {"index": 40, "reason": "not_json"}]}
```

| Answer | When |
| --- | --- |
| 200 | The batch was read. Samples that break the schema, or name another tenant or turbine, are refused one by one, and each refusal is kept with its reason and the start of what was sent (FR-TEL-003) |
| 401 `bad_credential` | No credential, a wrong or revoked one. Every way of failing gets the same answer |
| 429 `too_many_requests` | More than 600 requests in a minute from this device (SRS 7.5), with `Retry-After` |
| 503 `unavailable` | The rate counters cannot be reached, so the request is refused rather than let through |
| 400 `not_json`, `no_samples` | The body is not JSON, or has no `samples` list |
| 413 `too_large`, `too_many_samples` | More than 1 MiB, or more than 600 samples |

Errors are RFC 9457 problems, and never repeat what was sent. The route is served only over TLS, on port 8443: the
plain port, 8080, has health and metrics only. The gateway has no user sign-in on `/ingest/*`, only a coarse
per-address limit.

## What it stores

| Table | What | Partitions | Kept (SRS 8.3) |
| --- | --- | --- | --- |
| `operational_samples` | One row per 1 Hz sample: tenant, turbine, event time, sequence, the device, ingestion time, source, schema and model versions, `synthetic`, state, availability, quality status and the 12 readings. A reading is null while its sensor sends nothing. Quality status is `received` until data-quality detection labels it (M4.6) | A day each, by event time | 90 days |
| `scada_summaries` | A 10-minute SCADA summary per turbine: mean, minimum, maximum and standard deviation of each reading, coverage, quality status, availability, producing time, energy, duplicates, late samples, revision and the versions it came from | A month each, by period | 2 years |
| `ingest_rejections` | Each refused sample, with its reason and at most 500 characters of what was sent | None, deleted row by row | 90 days |
| `retention_holds` | Ranges of data a snapshot references, kept past retention until released. Snapshots write them from M4.7 | None | Until released |
| `device_credentials` | Each device's turbine, who made it, when, and when it was revoked. Only the secret's SHA-256 is kept | None | Always |

Every table and partition has forced row-level security, and a device's tenant comes from its credential, never from
what it sends.

### 10-minute summaries (FR-TEL-004)

The summarizer runs every minute. It reads which samples arrived since it last looked, by ingestion time, and
summarizes again each 10-minute period they belong to, from all its samples. So a delayed or out-of-order sample
recalculates its period, raising the summary's `revision` and counting it in `late_samples`. A period is first
summarized a minute after it ends, for batches still on their way. On its first run it reads back a day.

| Field | How |
| --- | --- |
| `sample_count`, `coverage` | Distinct samples by sequence, against 600 for 10 minutes at 1 Hz. A duplicate counts once, in `duplicate_samples` |
| `quality_status` | `complete` at 95% coverage or more, `partial` at 50%, otherwise `sparse` |
| `<reading>_mean`, `_min`, `_max`, `_std` | Over the samples where the reading is present. Missing readings are counted in `null_readings` |
| Wind direction | A circular mean and spread, so 350 and 10 degrees average to north |
| `energy_kwh` | The mean power over the whole 10 minutes, so a gap in the data does not read as no output |
| `available_fraction`, `producing_fraction` | The share of samples available, and producing |
| `source`, `schema_version`, `model_version`, `synthetic` | Where the samples came from, so a summary can be reproduced (FR-TEL-006) |

With more than one replica, an advisory lock lets one summarize at a time, and the others skip that minute.

### Partitions and retention (ADR 0003)

Every hour the service:

- makes partitions from yesterday to 3 days ahead, and from last month to 2 months ahead. Each table has a default
  partition for anything outside them, so nothing is lost. Before a partition is made, any rows the default partition
  holds for its range are moved into it, so PostgreSQL never refuses it;
- drops each partition wholly past its retention, unless a live retention hold, from any tenant, overlaps it. Rows in a
  default partition past retention, and rejections past it, are deleted, as there is no partition to drop.

A sample timed more than an hour ahead is refused as `event_time_in_future`, so a broken device clock cannot fill a
default partition.

| Setting | Default |
| --- | --- |
| `WINDOPS_TELEMETRY_RAW_RETENTION_DAYS` | `90`, for samples and rejections |
| `WINDOPS_TELEMETRY_SUMMARY_RETENTION_DAYS` | `730`, for summaries |

To run the upkeep now rather than on its schedule, inside the service's pod:

```bash
kubectl exec deployment/windops-telemetry -n windops-application -- \
  python -m windops_telemetry.upkeep --partitions --retention --summaries
```

## Device credentials

| Call | Needs |
| --- | --- |
| `CreateDeviceCredential` | `asset.manage` for the turbine's wind farm, and a second factor from the last 5 minutes. The turbine must exist in Asset. The token is in this first answer only: a retry with the same `idempotency_key` returns the credential without it |
| `ListDeviceCredentials` | `asset.manage` |
| `RevokeDeviceCredential` | As for making one. A revoked credential stops working at once |

Making and revoking a credential each queue an audit record. `make telemetry-devices` gives each demo turbine a
credential and keeps the tokens in the Secret `windops-simulator-devices`, for the simulator. It is safe to repeat:
a turbine whose token is in the Secret is skipped. The simulator's workload streams the whole demo fleet through
this route with those tokens; see [windops-simulator](../../simulator/README.md#streaming-live-m44).

## Still to come

- CMS vibration batches, with spectral band features and raw waveform windows (M4.5).
- Data-quality labels (M4.6).
- Reading telemetry and snapshots, `StreamTurbineTelemetry` and `CreateTelemetrySnapshot`, which answer
  UNIMPLEMENTED until M4.7. Snapshots will write retention holds.

## Run the tests

```bash
uv run pytest services/telemetry-service/tests
make telemetry-check           # on the cluster, after make telemetry-devices
make telemetry-storage-check   # partitions, summaries, late data and retention
```
