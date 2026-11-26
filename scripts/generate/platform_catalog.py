"""The platform catalog the administrator console shows read-only (M11.7, SRS 9.4): every MCP
server and tool with its limits and which agents may use it, and every gateway policy.

These are platform configuration, changed by pull request and deployed with a release, never by a
tenant. So the catalog is generated from their sources, committed into Web BFF's package, and a
repository test fails when it no longer matches them: a change shows in review, as the API's does.

    uv run python -m scripts.generate.platform_catalog
"""

import importlib
import json
from pathlib import Path
from typing import Any

from windops_identity.targets import AGENT_TOOLS, MCP_TOOLS, tool_scope
from windops_web_bff.limits import LIMITS, WINDOW_SECONDS

ROOT = Path(__file__).resolve().parents[2]
CATALOG = (
    ROOT / "services" / "web-bff-service" / "src" / "windops_web_bff" / "platform_catalog.json"
)
ROUTES = ROOT / "infrastructure" / "gateway" / "routes"
DEVELOPMENT = ROOT / "infrastructure" / "kubernetes" / "base" / "development" / "kustomization.yaml"

# What each server reaches, and the boundary it keeps (docs/mcp-integrations.md).
SERVERS = {
    "documentation": ("Knowledge Service", "Document permissions, checked by the service before "
                      "any text is returned"),
    "tavily": ("External Intelligence Service",
               "Generic web research only. It holds no Tavily key"),
    "weather": ("Weather Service", "Weather only, needing weather.read"),
    "windops": ("Asset, Telemetry, Alarm, Investigation, Production Impact, Policy, Inventory and "
                "Maintenance services", "The platform's own records, tenant-scoped; each service "
                "checks the user. Its one write is a draft work order"),
    "filesystem": ("Its own read-only sandboxes", "Synthetic documents and evaluation fixtures, "
                   "needing knowledge.read"),
    "postgres": ("PostgreSQL statistics views", "Read-only diagnostics, needing evaluation.manage"),
}  # fmt: skip
# What each gateway route is for, by its file.
ROUTE_PURPOSES = {
    "frontend": "The console itself: static files only",
    "ingest": "Telemetry and alarms from turbine devices, each with its own token",
    "keycloak-login": "Sign-in form posts, limited per address (SRS 7.5)",
    "keycloak": "Keycloak's sign-in pages and resources",
    "sample": "The Phase 1 sample service, for the gateway checks",
    "web-bff-public": "What a sign-in page reads before anyone signs in: branding and the API "
    "contract",
    "web-bff": "The browser's API: only with a valid Keycloak token for this console",
}


def _development_servers() -> set[str]:
    text = DEVELOPMENT.read_text()
    return {server for server in MCP_TOOLS if f"/{server}-mcp/" in text}


def mcp_servers() -> list[dict[str, Any]]:
    local_only = _development_servers()
    servers = []
    for server in sorted(MCP_TOOLS):
        module = importlib.import_module(f"windops_mcp_{server}.tools")
        calls, boundary = SERVERS[server]
        tools = []
        for tool in sorted(module.tools(object()), key=lambda t: t.name):
            scope = tool_scope(tool.server, tool.name)
            tools.append({
                "name": f"{tool.server}.{tool.name}", "title": tool.title,
                "description": tool.description.strip(),
                "timeout_seconds": tool.timeout_seconds, "per_minute": tool.per_minute,
                "per_day": tool.per_day, "external": tool.external, "read_only": tool.read_only,
                "agents": sorted(a for a, allowed in AGENT_TOOLS.items() if scope in allowed),
            })  # fmt: skip
        servers.append({
            "server": server, "workload": f"windops-mcp-{server}", "calls": calls,
            "boundary": boundary, "local_only": server in local_only, "tools": tools,
        })  # fmt: skip
    return servers


def _limit(plugin: dict[str, Any]) -> dict[str, Any]:
    return {"count": plugin["count"], "window_seconds": plugin["time_window"],
            "per": "address" if plugin.get("key") == "remote_addr" else str(plugin.get("key")),
            "rejected_code": plugin.get("rejected_code", 503)}  # fmt: skip


def gateway_routes() -> list[dict[str, Any]]:
    routes = []
    for path in sorted(ROUTES.glob("*.json")):
        route = json.loads(path.read_text())
        plugins = route.get("plugins", {})
        uris = route.get("uris") or [route["uri"]]
        upstream = route.get("upstream", {})
        cors = plugins.get("cors")
        routes.append({
            "name": path.stem,
            "purpose": ROUTE_PURPOSES.get(path.stem, ""),
            "uris": uris,
            "methods": route.get("methods") or ["any"],
            "sign_in": "Keycloak bearer token, checked for issuer, audience and tenant"
            if "openid-connect" in plugins else "none at the gateway",
            "limit": _limit(plugins["limit-count"]) if "limit-count" in plugins else None,
            "cors": {"origins": "the console's own origins only", "methods": cors["allow_methods"],
                     "credentials": cors.get("allow_credential", False)} if cors else None,
            "timeouts_seconds": upstream.get("timeout", {}),
            "upstream": [node.split(".")[0] for node in upstream.get("nodes", {})],
            "upstream_tls": upstream.get("scheme") == "https",
            "health_checked": "checks" in upstream,
            "traced": "opentelemetry" in plugins,
        })  # fmt: skip
    return routes


def generated() -> str:
    catalog = {
        "mcp_servers": mcp_servers(),
        "gateway_routes": gateway_routes(),
        "api_limits": {
            "window_seconds": WINDOW_SECONDS,
            "per_user": [{"traffic": name, "requests": count} for name, count in LIMITS.items()],
        },
    }
    return json.dumps(catalog, indent=2, sort_keys=True) + "\n"


def main() -> None:
    CATALOG.write_text(generated())
    print(f"Wrote {CATALOG.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
