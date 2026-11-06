"""docs/rbac.md shows the same default matrix the Authorization Service decides with."""

import re
from pathlib import Path

from windops_authorization import policy

DOC = Path(__file__).resolve().parent.parent / "docs" / "rbac.md"


def test_the_documented_matrix_is_the_policys() -> None:
    lines = DOC.read_text().splitlines()
    header = next(line for line in lines if line.startswith("| Permission | PA"))
    columns = [cell.strip() for cell in header.strip("|").split("|")][1:]
    assert [policy.ABBREVIATIONS[short] for short in columns] == list(policy.ROLES)
    documented: dict[str, set[str]] = {}
    for line in lines:
        found = re.match(r"\| `([a-z.]+)`[^|]*\|(.*)\|$", line)
        if found:
            cells = [cell.strip() for cell in found.group(2).split("|")]
            marked = zip(columns, cells, strict=True)
            documented[found.group(1)] = {
                policy.ABBREVIATIONS[short] for short, cell in marked if cell == "✓"
            }
    assert set(documented) == policy.PERMISSIONS
    for role in policy.ROLES:
        holds = {permission for permission, roles in documented.items() if role in roles}
        assert holds == policy.DEFAULT_MATRIX[role], role


def test_the_documented_platform_only_permissions_are_the_policys() -> None:
    marked = set(re.findall(r"\| `([a-z.]+)` \(platform only\)", DOC.read_text()))
    assert marked == policy.PLATFORM_ONLY
