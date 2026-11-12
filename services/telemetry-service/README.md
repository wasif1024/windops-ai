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
| `ingest_rejections` | Each refused sample, with its reason, at most 500 characters of what was sent, and its sequence and time if they can be read | None, deleted row by row | 90 days |
| `data_gaps` | Each run of sequences a turbine never sent, per 10-minute period | None, deleted row by row | 90 days |
| `retention_holds` | Ranges of data a snapshot references, kept past retention until released | None | Until released |
| `telemetry_snapshots` | Each snapshot: its turbine, window, who made it, when, its content hash, sample count and manifest, and when it was released | None | Always |
| `snapshot_samples` | Which stored samples each snapshot froze, by sequence, ingestion time and place in their batch, with each one's quality label then | None | With its snapshot |
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

### Data quality (FR-TEL-003)

Nothing a device sends is dropped silently. Each time the summarizer summarizes a period, it also labels the period's
samples, so labels follow late data the way summaries do. Each sample gets the first label that applies:

| `quality_status` | When |
| --- | --- |
| `duplicate` | A later copy of a sequence the turbine already sent. The first copy stays `ok` |
| `delayed` | It arrived more than `WINDOPS_TELEMETRY_DELAY_SECONDS`, 20 by default, after it was measured. A device sends within a few seconds |
| `out_of_order` | A sample with a higher sequence, from the same turbine, arrived before it. A delayed sample does not make the ones after it look out of order |
| `ok` | None of these |
| `received` | Not looked at yet: its period has not closed, or it was summarized before labelling existed |

Arrival order within one batch comes from each sample's place in it, `arrival_index`.

**Missing samples** cannot be labelled, as they are not there. Each run of sequences a turbine never sent is kept in
`data_gaps`, per period. A late sample that fills a gap removes it.

**Malformed samples** are the rejections the device route keeps, with their reasons. When a broken sample still has a
readable sequence and time, the rejection keeps them, so its slot is counted as malformed, not also as missing.
Text cut off part way cannot say which sample it was, so its slot reads as missing.

Each summary counts its `duplicate_samples`, `delayed_samples`, `out_of_order_samples`, `missing_samples` and
`malformed_samples`. A test replays a simulator scenario with every kind of problem through the route, and matches
the labels to its ground truth:
- duplicates, delays and gaps exactly;
- out-of-order with no false alarms. The only misses are a sample whose next one was itself lost, broken or delayed,
  when nothing out of order reached the wire.

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

### CMS vibration (FR-TEL-004)

`POST /ingest/v1/cms` takes one CMS batch: a window from each of up to three accelerometers (`main_bearing`,
`gearbox`, `generator`), with the shaft speeds when it was recorded. A window is 1,024 to 16,384 values in g, a power
of two long. A batch that breaks its schema, or names another tenant or turbine, is refused whole and kept with a
`cms_` reason, without its long windows in the excerpt. The answer says how many windows were stored, and how many
were `duplicates` of windows already kept.

Each window becomes one row of `cms_features`, computed off the event loop by a plain-Python FFT, version `1`:

| Feature | What |
| --- | --- |
| `rms`, `peak`, `crest_factor`, `kurtosis` | Time domain. Impacts from a damaged bearing raise kurtosis well above the 3 of healthy noise |
| `band_shaft_rms` | 0.5 to 2.5 orders of the sensor's shaft: imbalance and misalignment |
| `band_bearing_rms` | The watched bearing's outer-race defect frequency, plus or minus 10% |
| `band_gear_mesh_rms` | The gear mesh, plus or minus 5%, on the gearbox only |
| `band_broadband_rms`, `band_high_rms` | 10 Hz to 1 kHz, and 1 kHz to the Nyquist frequency |

Bands are placed from the drivetrain's kinematics and the shaft speed sent with the batch, so they follow the shaft as
the turbine speeds up. A stopped shaft's bands are empty, not zero. Every row carries `feature_version` and the
batch's model version, so an investigation can tell how its numbers were made (FR-TEL-006).

The raw window is kept in `cms_waveforms`, as float32 values, when the device flags a trigger, or for the first batch
in each block of `WINDOPS_TELEMETRY_CMS_RAW_EVERY_HOURS`, 6 by default. Keeping every window would take about 16 GB
over 90 days for the demo fleet; this keeps under 500 MB.

| Table | Partitions | Kept (SRS 8.3) |
| --- | --- | --- |
| `cms_features` | A month each, by event time | 2 years, `WINDOPS_TELEMETRY_CMS_FEATURE_RETENTION_DAYS` |
| `cms_waveforms` | A day each, by event time | 90 days, `WINDOPS_TELEMETRY_CMS_WAVEFORM_RETENTION_DAYS`, or longer while a snapshot holds them |

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

## Reading telemetry and snapshots (FR-TEL-006, SRS 8.4)

| Call | Needs | What it does |
| --- | --- | --- |
| `StreamTurbineTelemetry` | `telemetry.read` for the turbine | Streams its points for a window of at most 24 hours, in event order, every metric or the ones asked for. A duplicate is left out, as the first copy stays |
| `CreateTelemetrySnapshot` | `telemetry.read`, an idempotency key, a finished window | Freezes the window: which samples it held and their labels then, a content hash and a manifest, and a retention hold on its data |
| `GetTelemetrySnapshot` | `telemetry.read` | The snapshot and manifest, with its data read again and hashed again: `verified` when they match |
| `StreamTelemetrySnapshot` | `telemetry.read` | Its frozen points |
| `ReleaseTelemetrySnapshot` | `telemetry.read`, an idempotency key | Lets its hold go, so normal retention applies. It stays readable until its data is removed |

Each point has its metric, value, unit and sequence, and its quality: `GOOD` for an `ok` sample, `SUSPECT` for a
delayed, out-of-order or not-yet-checked one, and `MISSING`, with value 0, when the sensor sent nothing. The 12
metrics are the 12 readings; the first three are SRS 7.4's.

**What makes a snapshot reproducible.** It records the frozen samples' keys: sequence, ingestion time and place in
their batch. A stored sample never changes, so the keys find exactly what was read. Only samples stored before it
was made are in it, so data arriving late for its window reaches live reads, never the snapshot. Each sample's
quality label is kept as it was then, as later checks can relabel it. Its manifest holds:

| Field | What |
| --- | --- |
| `ingested_before` | The moment it was read: nothing stored later is in it |
| `sources` | Each source, schema version and model version, such as the simulator's, with its sample count |
| `quality` | How many samples had each data-quality label |
| `gaps`, `rejected` | Sequences never sent within it, and malformed samples refused for its window |
| `first_sequence`, `last_sequence` | The range it covers |
| `service_version`, `hash_format` | The release that made it, and how its hash is written |

The content hash is sha256 over a header line, `windops-telemetry-snapshot-v1|tenant|turbine|from|to`, then one line
per sample in event order: its time, sequence, label and the 12 readings in a fixed order, `|`-separated, with
`null` for a missing reading. Anyone with the samples can write it again to check a snapshot. Making and releasing
one each queue an audit record.

## Freshness (M4.9, SRS 10.3)

The Data Freshness dashboard and its alerts read two measures:

| Metric | What it is |
| --- | --- |
| `windops_telemetry_ingest_delay_seconds` | A histogram, by tenant: how long after it was measured each accepted sample was stored |
| `windops_telemetry_sample_age_seconds` | A gauge, by tenant and turbine: how old the turbine's newest sample is now |
| `windops_telemetry_turbines` | A gauge, by tenant and state: turbines `live`, `delayed`, `stale` or `silent` |
| `windops_telemetry_ingest_samples_total` | A counter: samples accepted, and refused by reason |

Each turbine with an active device credential is expected to send. Every 15 seconds the service reads each one's
newest event time through its `(tenant, turbine, event time)` index. The gauge works out the age when Prometheus reads
it, so a turbine that stops sending keeps getting older, which the delay alone cannot show. The steps match the
console: `live` up to a minute, `delayed` up to five minutes, then `stale`, and `silent` after a day with nothing.

| Alert | Fires when |
| --- | --- |
| `WindopsTelemetryStale` | A turbine's newest sample has been over 5 minutes old for 2 minutes |
| `WindopsTelemetrySilent` | A turbine with a device credential has sent nothing for a day |
| `WindopsTelemetryDelayed` | The 95th-percentile delay stays over 30 seconds for 10 minutes |
| `WindopsTelemetryRejected` | Over 50 samples are refused for one reason in 10 minutes |

## Run the tests

```bash
uv run pytest services/telemetry-service/tests
make telemetry-check           # on the cluster, after make telemetry-devices
make telemetry-storage-check   # partitions, summaries, late data and retention
make cms-check                 # live CMS features, a worn bearing's signature, raw windows
make data-quality-check        # one of each data problem, sent and labelled
make snapshot-check            # a live minute streamed, frozen, held, and unchanged by late data
make freshness-check           # live, stale and silent turbines, delay, refusals, and the dashboard
```
