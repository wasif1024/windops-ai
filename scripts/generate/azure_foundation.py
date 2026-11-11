#!/usr/bin/env python3
"""Generate what the Azure Bicep needs to know about the code (ADR 0006, M4A.6).

    uv run python -m scripts.generate.azure_foundation

The Bicep in infrastructure/azure/bicep loads these files, so Azure is made from the same facts
as the cluster, and a test fails until this runs again after they change:

- `workloads.json`: each workload with a Microsoft Entra identity, its namespace, and what it
  signs in to. A workload gets a role only on what its Deployment uses: Service Bus if it reads
  the events settings, PostgreSQL if it reads a database password, and Key Vault if it reads any
  other Secret. A consumer also gets to receive from its own subscription.
- `servicebus.json`: the topics and subscriptions, exactly as `windops_events.servicebus`
  defines them, so the Bicep creates the topology the code expects.
"""

import json
import sys
from pathlib import Path
from typing import Any

from windops_events.servicebus import (
    LOCK_SECONDS,
    SUBSCRIPTION_MAX_DELIVERIES,
    SUBSCRIPTIONS,
    filter_for,
    topics,
)
from windops_events.streams import DUPLICATE_WINDOW_SECONDS

from scripts.generate.azure_monitoring import CLUSTER_OUT
from scripts.generate.azure_overlay import REMOVED, base_documents, secret_references

REPOSITORY = Path(__file__).resolve().parents[2]
OUT = REPOSITORY / "infrastructure" / "azure" / "bicep" / "generated"
# The Collector that sends traces to Application Insights (M4A.5). It reads one Key Vault secret.
COLLECTOR = {"name": "windops-otel-collector", "namespace": "windops-observability"}
EVENT_SETTINGS = frozenset(name for name in REMOVED if name.startswith("WINDOPS_EVENTS_"))
DATABASE_SETTING = "WINDOPS_DATABASE_PASSWORD"


def iso_seconds(seconds: int) -> str:
    """Seconds as an ISO 8601 duration, as Service Bus takes them: 120 is PT2M."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    time = "".join(f"{v}{u}" for v, u in ((hours, "H"), (minutes, "M"), (secs, "S")) if v)
    return "P" + (f"{days}D" if days else "") + (f"T{time}" if time else "") or "PT0S"


def workloads() -> list[dict[str, Any]]:
    deployments = sorted(
        (d for d in base_documents() if d["kind"] == "Deployment" and secret_references(d)),
        key=lambda d: d["metadata"]["name"],
    )
    consumers = {f"windops-{s.durable}": s for s in SUBSCRIPTIONS}
    out = []
    for deployment in deployments:
        name = deployment["metadata"]["name"]
        variables = {variable for _, variable, _, _ in secret_references(deployment)}
        subscription = consumers.pop(name, None)
        out.append(
            {
                "name": name,
                "namespace": deployment["metadata"]["namespace"],
                "events": bool(variables & EVENT_SETTINGS),
                "database": DATABASE_SETTING in variables,
                "keyVault": bool(variables - REMOVED),
                "receives": (
                    [{"topic": subscription.topic, "subscription": subscription.durable}]
                    if subscription
                    else []
                ),
            }
        )
    if consumers:
        raise ValueError(f"Subscriptions with no workload to read them: {sorted(consumers)}")
    collector = (CLUSTER_OUT / "collector.yaml").read_text()
    if COLLECTOR["name"] not in collector:
        raise ValueError("The Azure Collector has a different name: update COLLECTOR")
    out.append({**COLLECTOR, "events": False, "database": False, "keyVault": True, "receives": []})
    return out


def servicebus() -> dict[str, Any]:
    return {
        "topics": [
            {
                "name": name,
                "duplicateWindow": iso_seconds(DUPLICATE_WINDOW_SECONDS),
                "messageTimeToLive": iso_seconds(spec.max_age_seconds),
                "maxSizeInMegabytes": 1024,
            }
            for name, spec in topics()
        ],
        "subscriptions": [
            {
                "topic": s.topic,
                "name": s.durable,
                "filter": filter_for(s.subject),
                "maxDeliveryCount": SUBSCRIPTION_MAX_DELIVERIES,
                "lockDuration": iso_seconds(LOCK_SECONDS),
            }
            for s in SUBSCRIPTIONS
        ],
    }


def _json(body: Any) -> str:
    return json.dumps(body, indent=2) + "\n"


def generate() -> dict[Path, str]:
    return {
        OUT / "workloads.json": _json(workloads()),
        OUT / "servicebus.json": _json(servicebus()),
    }


def main() -> int:
    files = generate()
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    print(f"Wrote {len(files)} files for the Azure Bicep, in {OUT.relative_to(REPOSITORY)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
