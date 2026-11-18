"""The services a user's token may be exchanged for, and each MCP server's tool scopes (SEC-008).

An exchanged token names one service, lasts 120 seconds, and for an MCP server carries only the
tools asked for, each as its own scope. Keycloak's client scopes are seeded from this, so a scope
that is not here cannot be issued (MCP-001, MCP-004).
"""

AGENT_RUNTIME = "windops-agent-runtime"
MCP_PREFIX = "windops-mcp-"
# Each MCP server's tools, by the short name its scopes use (SRS 6.3).
MCP_TOOLS: dict[str, tuple[str, ...]] = {
    "documentation": ("search_documents", "read_passage"),
    "tavily": ("search_web", "extract_page", "get_research"),
    "weather": ("get_forecast",),
    # Development only, never deployed on Azure.
    "filesystem": ("list_files", "read_file"),
    "postgres": ("database_overview", "connection_activity"),
}


def mcp_audience(server: str) -> str:
    return f"{MCP_PREFIX}{server}"


def tool_scope(server: str, tool: str) -> str:
    return f"mcp.{server}.{tool}"


# By audience: the tool scopes that may be asked for, none for a service that is not a tool server.
EXCHANGE_TARGETS: dict[str, frozenset[str]] = {
    AGENT_RUNTIME: frozenset(),
    **{
        mcp_audience(server): frozenset(tool_scope(server, tool) for tool in tools)
        for server, tools in MCP_TOOLS.items()
    },
}


def audience_scope(audience: str) -> str:
    """The client scope that names one target in a token's audience."""
    return f"{audience}-audience"
