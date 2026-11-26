"""The screens each role's menu shows, for the Phase 11 gate's role-based browser tests (M11.8).

Worked out from the two sources that decide it: the SRS 3.2 matrix in the Authorization Service's
policy, and the permissions each console screen needs in frontend/src/app/navigation.ts. Every
browser journey checks its role's menu against this file, and a repository test keeps the file
matching its sources.

    uv run python -m scripts.generate.role_menus
"""

import json
import re
from pathlib import Path

from windops_authorization.policy import DEFAULT_MATRIX, ROLES

ROOT = Path(__file__).resolve().parents[2]
NAVIGATION = ROOT / "frontend" / "src" / "app" / "navigation.ts"
MENUS = ROOT / "frontend" / "e2e" / "role-menus.json"
_SCREEN = re.compile(
    r'\{\s*id:\s*"(?P<id>[a-z-]+)",\s*title:\s*"(?P<title>[^"]+)",\s*anyOf:\s*\[(?P<any>[^\]]*)\]',
    re.S,
)


def screens() -> list[tuple[str, str, frozenset[str]]]:
    """Each screen in menu order: its ID, title and the permissions any one of which shows it."""
    text = NAVIGATION.read_text()
    body = text[text.index("export const SCREENS") :]
    found = [
        (m["id"], m["title"], frozenset(re.findall(r'"([a-z_.]+)"', m["any"])))
        for m in _SCREEN.finditer(body)
    ]
    if not found:
        raise ValueError("No screens found in navigation.ts")
    return found


def menus() -> dict[str, list[str]]:
    return {
        role: [title for _, title, any_of in screens()
               if not any_of or any_of & DEFAULT_MATRIX[role]]
        for role in ROLES
    }  # fmt: skip


def generated() -> str:
    return json.dumps(menus(), indent=2) + "\n"


def main() -> None:
    MENUS.write_text(generated())
    print(f"Wrote {MENUS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
