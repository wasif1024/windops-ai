"""Check that GitHub Actions workflows follow the pinning rule in SRS section 9.5."""

import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))


def test_workflows_exist() -> None:
    assert WORKFLOWS


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_full_commit(workflow: Path) -> None:
    actions = re.findall(r"uses:\s*(\S+)", workflow.read_text())
    for action in actions:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), action
