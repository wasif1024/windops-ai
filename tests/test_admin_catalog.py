"""The console's lists of roles and permissions match the Authorization Service's policy."""

import re
from pathlib import Path

from windops_authorization import policy

CATALOG = Path(__file__).resolve().parent.parent / "frontend/src/features/admin/catalog.ts"


def _strings(block_name: str) -> list[str]:
    text = CATALOG.read_text()
    block = re.search(rf"export const {block_name}[^=]*= \[(.*?)\]", text, re.DOTALL)
    assert block, block_name
    return re.findall(r'"([a-z.-]+)"', block.group(1))


def test_the_roles_are_the_policys_in_its_order() -> None:
    ids = re.findall(r'id: "([a-z-]+)"', CATALOG.read_text())
    assert tuple(ids) == policy.ROLES


def test_the_permissions_are_the_policys() -> None:
    permissions = _strings("PERMISSIONS")
    assert len(permissions) == len(set(permissions))
    assert set(permissions) == policy.PERMISSIONS


def test_platform_only_permissions_match() -> None:
    assert set(_strings("PLATFORM_ONLY")) == policy.PLATFORM_ONLY
