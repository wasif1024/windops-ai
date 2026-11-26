"""The menus the Phase 11 gate's browser tests expect each role to see match the SRS 3.2 matrix and
the console's own navigation (M11.8)."""

import json

from scripts.generate.role_menus import MENUS, generated, screens


def test_the_expected_menus_match_their_sources() -> None:
    assert MENUS.read_text() == generated(), "Run: uv run python -m scripts.generate.role_menus"


def test_every_screen_is_on_some_roles_menu_and_the_platform_on_one_only() -> None:
    menus = json.loads(MENUS.read_text())
    shown = {title for titles in menus.values() for title in titles}
    assert shown == {title for _, title, _ in screens()}
    assert [role for role, titles in menus.items() if "Platform" in titles] == [
        "platform-administrator"
    ]
    assert "Administration" not in menus["auditor"], "an auditor reviews in the audit explorer"
