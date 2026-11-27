#!/usr/bin/env python3
"""The scenario pipeline (M12.2, SRS 10.5, EVL-005, EVL-006, SIM-005).

Scenarios are written in evaluations/scenarios and listed by a versioned dataset manifest in
evaluations/datasets. This takes a dataset version from the repository to where it is used:

1. **Checked:** every scenario against the simulator, the agents' tools and its family's runs, and
   the set for EVL-006: each replay, a simulator scenario with its seed, belongs to one split, so
   no held-out telemetry is ever seen while prompts are tuned.
2. **Its ground truth exported:** for each scenario, the simulator's own record of what happened,
   for its seed: the fault, its component, severity and timing. It goes into the scenario's
   definition for the Evaluation Service, never to an agent (SIM-005).
3. **Locked:** the manifest keeps each version's content hash, the same hash the Evaluation
   Service computes. A change to any scenario without a new version fails here, before the
   service would refuse it (EVL-005). `--lock` records a new version's hash, and never rewrites an
   old one.
4. **Registered** with the Evaluation Service, which keeps the version unchanged for good.
5. **Synchronized to LangSmith:** each scenario an example in its split, the ground truth in its
   outputs, never its inputs, and the dataset tagged with the version.

    make scenarios                        # check, register and synchronize
    make scenarios-lock                   # record the hash of a new version in its manifest
    uv run python -m scripts.evaluation.pipeline --check   # check and lock only: no cluster
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from windops.evaluation.v1 import evaluation_pb2 as pb
from windops_evaluation import rules
from windops_simulator.scenarios import GroundTruth

from scripts.evaluation.scenarios import (
    ROOT,
    Scenario,
    ScenarioError,
    dataset,
    recording,
    simulated,
)
from scripts.lib import settings

DEFAULT_DATASET = "first-regression"


def info(message: str) -> None:
    print(f"info - {message}", flush=True)


def ground_truth(scenario: Scenario) -> dict[str, Any]:
    """What the simulator says happened in this scenario's replay, for its seed (SIM-005). The
    data problems are drawn while it replays, so they are left to the replay."""
    truth = GroundTruth(simulated(scenario)).as_dict()
    truth.pop("data_problems", None)
    return truth


def definition(scenario: Scenario, root: Path = ROOT) -> str:
    """The scenario as written, with the simulator's ground truth and its recording, as the
    service keeps it: a changed recording is a changed dataset (EVL-005)."""
    written = scenario.model_dump(mode="json")
    written["simulator_ground_truth"] = ground_truth(scenario)
    recorded = recording(scenario, root)
    if recorded is not None:
        written["recording"] = recorded.model_dump(mode="json")
    return json.dumps(written, sort_keys=True, separators=(",", ":"))


def to_message(scenario: Scenario) -> pb.Scenario:
    return pb.Scenario(
        scenario_id=scenario.case_id, family=scenario.family, split=scenario.split,
        severity=scenario.severity, runs_required=scenario.runs_required,
        definition_json=definition(scenario),
    )  # fmt: skip


def content_hash(scenarios: Sequence[Scenario], root: Path = ROOT) -> str:
    """The hash the Evaluation Service gives this content, worked out the same way."""
    return rules.content_hash([
        rules.ScenarioSpec(s.case_id, s.family, s.split, s.severity, s.runs_required,
                           definition(s, root))
        for s in scenarios
    ])  # fmt: skip


def manifest_path(name: str, root: Path = ROOT) -> Path:
    return root / "datasets" / f"{name}.json"


def check_lock(name: str = DEFAULT_DATASET, root: Path = ROOT) -> tuple[dict[str, str], str]:
    """The manifest's header and the content's hash, once its version's lock matches it."""
    header, scenarios = dataset(name, root)
    digest = content_hash(scenarios, root)
    versions: Mapping[str, str] = json.loads(manifest_path(name, root).read_text()).get(
        "versions", {}
    )
    locked = versions.get(header["version"])
    if locked is None:
        raise ScenarioError(
            f"{name} version {header['version']} is not locked: run `make scenarios-lock`"
        )
    if locked != digest:
        raise ScenarioError(
            f"{name}'s scenarios changed since version {header['version']} was locked: give the "
            "dataset a new version, then run `make scenarios-lock` (EVL-005)"
        )
    return header, digest


def lock(name: str = DEFAULT_DATASET, root: Path = ROOT) -> str:
    """Record the current version's hash. An old version's hash is never rewritten."""
    path = manifest_path(name, root)
    manifest = json.loads(path.read_text())
    _, scenarios = dataset(name, root)
    digest = content_hash(scenarios, root)
    versions: dict[str, str] = manifest.setdefault("versions", {})
    version = str(manifest["version"])
    if versions.get(version, digest) != digest:
        raise ScenarioError(
            f"{name} version {version} is locked with other content: give it a new version first"
        )
    versions[version] = digest
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return digest


def langsmith_inputs(s: Scenario) -> dict[str, Any]:
    """What a person would give: never the ground truth (SIM-005)."""
    return {"title": s.case.title, "severity": s.case.severity,
            "replay": f"{s.input_fixture}@{s.simulator_seed}"}  # fmt: skip


def langsmith_outputs(s: Scenario) -> dict[str, Any]:
    return {
        "ground_truth": s.ground_truth,
        "simulator_ground_truth": ground_truth(s),
        "expected_behavior": s.expected_behavior,
        "prohibited_behavior": s.prohibited_behavior,
        "expected_outcome": s.expected_outcome,
        "expect": s.expect.model_dump(),
    }


def to_langsmith(header: Mapping[str, str], scenarios: Sequence[Scenario]) -> Any:
    """Each scenario an example in its split, and the dataset tagged with this version. Returns
    LangSmith's client and dataset, or None without a key."""
    from langsmith import Client

    values = settings.load()
    if not values.get("LANGSMITH_API_KEY"):
        info("no LANGSMITH_API_KEY: the dataset stays here and in the Evaluation Service")
        return None
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    client = Client(api_key=values["LANGSMITH_API_KEY"])
    name = header["dataset"]
    if client.has_dataset(dataset_name=name):
        found = client.read_dataset(dataset_name=name)
    else:
        found = client.create_dataset(name, description=header.get("description", ""),
                                      metadata={"version": header["version"]})  # fmt: skip
    existing = {str(e.metadata.get("scenario_id")): e
                for e in client.list_examples(dataset_id=found.id) if e.metadata}  # fmt: skip
    for s in scenarios:
        metadata = {"scenario_id": s.case_id, "family": s.family, "dataset_version":
                    header["version"], "simulator_seed": s.simulator_seed}  # fmt: skip
        inputs, outputs = langsmith_inputs(s), langsmith_outputs(s)
        if s.case_id in existing:
            client.update_example(existing[s.case_id].id, inputs=inputs, outputs=outputs,
                                  metadata=metadata, split=s.split)  # fmt: skip
        else:
            client.create_examples(dataset_id=found.id, examples=[
                {"inputs": inputs, "outputs": outputs, "metadata": metadata, "split": s.split}
            ])  # fmt: skip
    # The version names this state of the dataset, so an experiment can be traced to it.
    client.update_dataset_tag(dataset_id=found.id, as_of=datetime.now(UTC),
                              tag=f"v{header['version']}")  # fmt: skip
    info(f"synchronized {len(scenarios)} scenarios to LangSmith's {name}, tagged "
         f"v{header['version']}")  # fmt: skip
    return client, found


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--lock", action="store_true", help="record this version's hash")
    parser.add_argument("--check", action="store_true", help="check and lock only")
    parser.add_argument("--no-langsmith", action="store_true", help="keep it off LangSmith")
    arguments = parser.parse_args(argv)
    try:
        if arguments.lock:
            digest = lock(arguments.dataset)
            info(f"locked {arguments.dataset} at {digest[:12]}")
        header, digest = check_lock(arguments.dataset)
        _, scenarios = dataset(arguments.dataset)
        splits = {split: sum(s.split == split for s in scenarios) for split in rules.SPLITS}
        info(f"{header['dataset']} version {header['version']}: {len(scenarios)} scenarios "
             f"({', '.join(f'{n} {k}' for k, n in splits.items())}), each replay in one split, "
             f"content {digest[:12]}")  # fmt: skip
        if arguments.check or arguments.lock:
            return 0
        from scripts.evaluation.service import evaluations

        with evaluations() as api:
            registered = api.register(header, scenarios)
        if registered.content_sha256 != digest:
            raise ScenarioError("The Evaluation Service's hash differs from the lock")
        info(f"registered with the Evaluation Service: {registered.name} "
             f"{registered.version}")  # fmt: skip
        if not arguments.no_langsmith:
            to_langsmith(header, scenarios)
    except (ScenarioError, OSError, ValueError, KeyError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Scenario pipeline passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
