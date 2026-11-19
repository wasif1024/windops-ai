"""The registered tasks: the only code the Sandbox ever runs (SBX-001).

Each task is a folder under `tasks/`: `task.yaml` gives its name, version, limits and the JSON
Schemas of its input and output, and `main.py` is its code, which reads its input as JSON on
stdin and prints one JSON object. Both are read once when the service starts, and the code's hash
goes with every result (SBX-003). An agent names a task and gives it input; it never sends code.
"""

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

_NAME = re.compile(r"[a-z][a-z0-9_]{2,40}")
# Bounds no task's own limits may pass, whatever its file says.
CEILING = {"input_bytes": 1 << 20, "seconds": 120, "memory_mb": 2048, "output_bytes": 1 << 16}


class TaskInputError(ValueError):
    """The input is too large, not JSON, or breaks the task's schema."""


@dataclass(frozen=True)
class Limits:
    input_bytes: int
    seconds: int
    memory_mb: int
    output_bytes: int


@dataclass(frozen=True)
class Task:
    name: str
    version: int
    description: str
    code: str
    code_sha256: str
    limits: Limits
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    def checked_input(self, raw: str) -> str:
        """The input, as canonical JSON, if it is within the task's limit and schema."""
        if len(raw.encode()) > self.limits.input_bytes:
            raise TaskInputError(f"The input is over {self.limits.input_bytes} bytes")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise TaskInputError("The input is not JSON") from error
        _validate(self.input_schema, value, "input")
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def checked_output(self, raw: str) -> dict[str, Any]:
        """The output, if it is one JSON object that passes the task's schema."""
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise TaskInputError("The output is not JSON") from error
        _validate(self.output_schema, value, "output")
        if not isinstance(value, dict):
            raise TaskInputError("The output is not an object")
        return value


def _validate(schema: dict[str, Any], value: Any, what: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(part) for part in first.absolute_path) or "the top level"
        # The schema's complaint names a field and a rule, never echoes a large value.
        raise TaskInputError(f"The {what} breaks its schema at {where}: {first.validator}")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_task(folder: Path) -> Task:
    spec = yaml.safe_load((folder / "task.yaml").read_text())
    code = (folder / "main.py").read_text()
    name = str(spec["name"])
    if not _NAME.fullmatch(name) or name != folder.name:
        raise ValueError(f"{folder}: a task's name must be its folder's, in lowercase")
    limits = Limits(**{key: int(spec["limits"][key]) for key in CEILING})
    for key, ceiling in CEILING.items():
        value = getattr(limits, key)
        if not 0 < value <= ceiling:
            raise ValueError(f"{name}: its {key} limit must be above 0 and at most {ceiling}")
    for schema in (spec["input_schema"], spec["output_schema"]):
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError(f"{name}: its schemas must be closed objects")
    return Task(
        name=name, version=int(spec["version"]),
        description=" ".join(str(spec["description"]).split()), code=code,
        code_sha256=sha256(code), limits=limits,
        input_schema=spec["input_schema"], output_schema=spec["output_schema"],
    )  # fmt: skip


def tasks_root() -> Path:
    configured = os.environ.get("WINDOPS_SANDBOX_TASKS")
    if configured:
        return Path(configured)
    # In the repository, the service's own tasks folder.
    return Path(__file__).resolve().parents[2] / "tasks"


@cache
def registry(root: Path | None = None) -> Mapping[str, Task]:
    folder = root or tasks_root()
    found = {
        task.name: task
        for task in (load_task(path) for path in sorted(folder.iterdir()) if path.is_dir())
    }
    if not found:
        raise ValueError(f"No tasks are registered in {folder}")
    return found
