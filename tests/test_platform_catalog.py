"""The administrator console's platform catalog matches the MCP servers, the agents' allowlists and
the gateway routes it is made from, so a change to any of them shows in review (M11.7)."""

import json

from scripts.generate.platform_catalog import CATALOG, generated


def test_the_catalog_matches_its_sources() -> None:
    assert CATALOG.read_text() == generated(), (
        "Run: uv run python -m scripts.generate.platform_catalog"
    )


def test_development_servers_are_marked_and_every_tool_has_its_limits() -> None:
    catalog = json.loads(CATALOG.read_text())
    local = {s["server"] for s in catalog["mcp_servers"] if s["local_only"]}
    assert local == {"filesystem", "postgres"}
    tools = [t for s in catalog["mcp_servers"] for t in s["tools"]]
    assert tools and all(
        t["timeout_seconds"] > 0 and t["per_day"] >= t["per_minute"] for t in tools
    )
    writers = [t["name"] for t in tools if not t["read_only"]]
    assert writers == ["windops.create_draft_work_order"]


def test_the_api_route_needs_a_token_and_sign_in_is_limited() -> None:
    routes = {r["name"]: r for r in json.loads(CATALOG.read_text())["gateway_routes"]}
    assert routes["web-bff"]["sign_in"].startswith("Keycloak bearer token")
    assert routes["keycloak-login"]["limit"]["count"] == 5
    assert not routes["web-bff"]["cors"]["credentials"], "never credentialed CORS (SRS 9.5)"
