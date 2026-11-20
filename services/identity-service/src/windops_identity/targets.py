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
    # The platform's own records, tenant-scoped (M8.6), and draft work orders (M9.1).
    "windops": (
        "get_turbine",
        "get_latest_telemetry",
        "get_telemetry_summary",
        "list_alarms",
        "get_case",
        "list_cases",
        "get_evidence",
        "estimate_impact",
        "check_policy",
        # The one write an agent may make: a draft (FR-MNT-003, M9.1).
        "create_draft_work_order",
    ),
    # Development only, never deployed on Azure.
    "filesystem": ("list_files", "read_file"),
    "postgres": ("database_overview", "connection_activity"),
}


# The Agent Sandbox Adapter's registered tasks (ADR 0024). Not MCP: the runtime calls the adapter
# by its own certificate, and gives a task only to an agent whose allowlist names it. So no agent
# token is ever minted for one. A test keeps this matched to the adapter's tasks folder.
SANDBOX_TASKS: tuple[str, ...] = ("vibration_spectrum", "trend_fit")


def sandbox_scope(task: str) -> str:
    return f"sandbox.{task}"


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


# Tools that return content from outside the platform, which is untrusted (MCP-005), and tools
# that change a record. Each MCP server's own definitions say the same: a test keeps them matched.
EXTERNAL_TOOLS = frozenset(
    {
        tool_scope("documentation", "search_documents"),
        tool_scope("documentation", "read_passage"),
        tool_scope("tavily", "search_web"), tool_scope("tavily", "extract_page"),
        tool_scope("tavily", "get_research"),
        tool_scope("filesystem", "read_file"),
    }
)  # fmt: skip
# None yet: create_draft arrives with the Maintenance Service (Phase 9), for the planner alone.
# Tools that change something. No agent that reads external content may hold one (MCP-006).
WRITE_TOOLS: frozenset[str] = frozenset({tool_scope("windops", "create_draft_work_order")})


def _tools(server: str, *names: str) -> frozenset[str]:
    return frozenset(tool_scope(server, name) for name in names)


# Each agent's explicit allowlist (SRS 5.3, MCP-002). The Identity Service mints an agent's token
# only for tools on its list, so text an agent reads can never widen it (MCP-007).
AGENT_TOOLS: dict[str, frozenset[str]] = {
    # Plans, delegates, reads the investigation, and checks policy. Never approves.
    "supervisor": _tools("windops", "get_case", "list_cases", "get_evidence", "check_policy"),
    # Telemetry, alarms and turbines. Cannot change telemetry or thresholds.
    "scada-analyst": _tools(
        "windops",
        "get_turbine",
        "get_latest_telemetry",
        "get_telemetry_summary",
        "list_alarms",
        "get_evidence",
    ),
    # Knowledge, fault history and Sandbox analysis. Cites and states uncertainty.
    "reliability-engineer": _tools("documentation", "search_documents", "read_passage")
    | _tools("windops", "get_turbine", "list_cases", "get_evidence", "get_telemetry_summary")
    | frozenset(sandbox_scope(task) for task in SANDBOX_TASKS),
    # Production Impact's deterministic results, to explain, never to change (FR-IMP-004).
    "production-impact": _tools("windops", "get_case", "get_turbine", "estimate_impact"),
    # Maintenance history and draft work orders (inventory comes in M9.2). Never reads raw web
    # content, so it may hold the one write tool (MCP-006).
    "maintenance-planner": _tools(
        "windops", "get_case", "list_cases", "get_turbine", "create_draft_work_order"
    ),
    # Procedures, external research and weather. Read-only; cannot declare a site safe.
    "safety-researcher": _tools("documentation", "search_documents", "read_passage")
    | _tools("tavily", "search_web", "extract_page", "get_research")
    | _tools("weather", "get_forecast"),
}


def check_allowlists() -> None:
    """Every allowed tool exists, and no agent that reads external content may write (MCP-006)."""
    known = {tool_scope(server, tool) for server, tools in MCP_TOOLS.items() for tool in tools}
    known |= {sandbox_scope(task) for task in SANDBOX_TASKS}
    for agent, tools in AGENT_TOOLS.items():
        unknown = tools - known
        if unknown:
            raise ValueError(f"{agent} is allowed tools that do not exist: {sorted(unknown)}")
        if tools & EXTERNAL_TOOLS and tools & WRITE_TOOLS:
            raise ValueError(f"{agent} reads external content, so it may not write (MCP-006)")
        if any(scope.startswith(tool_scope("filesystem", "")) for scope in tools) or any(
            scope.startswith(tool_scope("postgres", "")) for scope in tools
        ):
            raise ValueError(f"{agent}: development servers are never an agent's tools")


check_allowlists()
