"""Every SRS 7.7 breaker and fallback, and the fault-injection tests that prove it (RES-005).

Each row of SRS 7.7's "Dependency down" table has an entry here, named exactly as the table
names it. An entry lists the tests that take that dependency down, by a breaker, a bulkhead, a
deadline, an unreachable address or an error, and check the defined behavior. A behavior not built
yet names the module that builds it instead, as `gap`. `tests/test_fallbacks.py` keeps the table,
this list and the tracker in step, and `make chaos` runs every test listed here.
"""

from dataclasses import dataclass

BREAKER = "shared/resilience/tests/test_breaker.py"
OUTBOX = "shared/outbox/tests/test_outbox.py"
EVENTS = "shared/events/tests/test_event_client.py"
CACHE = "shared/cache/tests/test_cache.py"
BFF_OUTAGES = "services/web-bff-service/tests/test_outages.py"
EVIDENCE = "services/investigation-service/tests/test_evidence.py"
POLICY = "services/policy-service/tests/test_windops_policy.py"
MAINTENANCE = "services/maintenance-service/tests/test_windops_maintenance.py"
APPROVAL = "services/approval-service/tests/test_windops_approval.py"
WEATHER = "services/weather-service/tests"
IDENTITY = "services/identity-service/tests/test_identity_service.py"
KNOWLEDGE = "services/knowledge-service/tests"
RUNTIME = "services/agent-runtime-service/tests"


@dataclass(frozen=True)
class Fallback:
    """One SRS 7.7 row: its tests as `path::function`, or the module that closes its gap."""

    dependency: str
    tests: tuple[str, ...] = ()
    gap: str = ""
    note: str = ""


# The breaker itself: it opens, refuses, probes and closes as SRS 7.7 sets, for every dependency.
BREAKER_TESTS = (
    f"{BREAKER}::test_opens_at_half_failures_with_the_minimum_calls",
    f"{BREAKER}::test_slow_successes_count_as_failures",
    f"{BREAKER}::test_caller_errors_never_open_the_breaker",
    f"{BREAKER}::test_open_breaker_refuses_without_calling",
    f"{BREAKER}::test_half_open_only_after_the_open_duration",
    f"{BREAKER}::test_three_successful_probes_close_the_breaker",
    f"{BREAKER}::test_any_failed_probe_reopens_the_breaker",
    f"{BREAKER}::test_state_changes_are_logged_and_reported",
    "shared/resilience/tests/test_retry.py::test_open_breaker_is_not_retried",
    "shared/resilience/tests/test_real_grpc.py::test_real_outage_retries_then_opens_the_breaker",
    "shared/resilience/tests/test_bulkhead.py::test_a_slow_dependency_leaves_others_untouched",
    "shared/resilience/tests/test_bulkhead.py::test_a_refusal_does_not_count_against_the_dependency",
    "shared/grpc/tests/test_client.py::test_idempotent_unavailable_is_retried_and_opens_the_breaker",
    "shared/observability/tests/test_tracing.py::test_real_breaker_exports_its_state",
    "services/sample-caller-service/tests/test_sample_flow.py::test_outage_returns_503_then_the_breaker_opens",
    "services/sample-caller-service/tests/test_sample_flow.py::test_caller_stays_ready_during_an_outage",
)

FALLBACKS = (
    Fallback(
        "Anthropic API",
        gap="M13.2b",
        note="No breaker guards Claude, and an outage retries the run until it fails; "
        "SRS 7.7 pauses it, resumable, and tells the operator.",
    ),
    Fallback(
        "Tavily",
        (
            "services/external-intelligence-service/tests/test_external_intelligence_service.py"
            "::test_turned_off_over_quota_or_unreachable_is_a_recorded_data_gap",
        ),
    ),
    Fallback(
        "Forecast provider",
        (
            f"{WEATHER}/test_provider.py::test_no_connection_is_unavailable",
            f"{WEATHER}/test_provider.py::test_a_busy_or_failing_provider_counts_against_its_breaker",
            f"{WEATHER}/test_windops_weather.py"
            "::test_during_an_outage_old_data_is_marked_stale_and_never_presented_as_current",
            "services/policy-service/tests/test_rules.py"
            "::test_a_missing_unavailable_stale_or_wrong_input_fails_closed",
        ),
    ),
    Fallback(
        "Knowledge or Qdrant",
        (f"{KNOWLEDGE}/test_retrieval.py::test_without_its_dependencies_a_search_returns_nothing",),
    ),
    Fallback(
        "Telemetry or Alarm",
        (
            f"{EVIDENCE}::test_without_every_source_there_is_no_snapshot_and_evidence_stays_pending",
            "services/web-bff-service/tests/test_fleet.py"
            "::test_the_fleet_is_still_shown_when_telemetry_cannot_be_reached",
        ),
    ),
    Fallback(
        "Policy",
        (f"{MAINTENANCE}::test_the_policy_service_blocks_or_cannot_be_asked",),
    ),
    Fallback(
        "Authorization",
        (f"{IDENTITY}::test_it_fails_closed_when_authorization_is_down",),
    ),
    Fallback(
        "Approval",
        (f"{BFF_OUTAGES}::test_approval_down_rejects_decisions",),
        note="The queue lives in the Approval Service, so it cannot be read while it is down.",
    ),
    Fallback(
        "Audit",
        (
            f"{OUTBOX}::test_an_outage_keeps_the_event_for_later",
            f"{OUTBOX}::test_the_backlog_is_exported_as_metrics",
            "services/audit-service/tests/test_windops_audit.py"
            "::test_a_database_outage_makes_the_event_wait",
        ),
        note="WindopsOutboxBacklog alerts on the backlog.",
    ),
    Fallback(
        "Notification",
        (f"{EVENTS}::test_a_dependency_outage_never_dead_letters_an_event",),
        note="Nothing calls the Notification Service: it reads events, which wait for it.",
    ),
    Fallback(
        "LangSmith",
        gap="M13.2b",
        note="The LangSmith client buffers and drops traces, but no alert says so.",
    ),
    Fallback(
        "Sandbox",
        (
            f"{RUNTIME}/test_team.py::test_an_unavailable_sandbox_tells_the_agent_to_carry_on",
            "services/agent-sandbox-adapter/tests/test_agent_sandbox.py"
            "::test_an_unavailable_sandbox_is_unavailable_and_opens_the_breaker",
        ),
    ),
    Fallback(
        "Identity",
        (f"{BFF_OUTAGES}::test_identity_down_keeps_sessions_and_refuses_user_changes",),
        note="Only administration asks Identity, so every other screen already shows user IDs.",
    ),
    Fallback(
        "Keycloak",
        (f"{IDENTITY}::test_a_keycloak_outage_refuses_the_change",),
    ),
    Fallback(
        "Tenant",
        (f"{BFF_OUTAGES}::test_tenant_down_refuses_tenant_changes",),
        gap="M13.2b",
        note="Tenant changes are refused, but the console falls back to the default branding, "
        "not the last known.",
    ),
    Fallback(
        "Asset",
        (
            f"{EVIDENCE}::test_without_every_source_there_is_no_snapshot_and_evidence_stays_pending",
            f"{POLICY}::test_an_owner_that_cannot_be_reached_fails_closed",
        ),
    ),
    Fallback(
        "Fault Detection",
        (f"{EVENTS}::test_a_dependency_outage_never_dead_letters_an_event",),
        note="Telemetry never calls Fault Detection; WindopsConsumerLag alerts on the delay.",
    ),
    Fallback(
        "Investigation",
        (
            f"{APPROVAL}::test_a_decision_fails_closed_when_it_cannot_be_rechecked",
            f"{EVENTS}::test_a_dependency_outage_never_dead_letters_an_event",
        ),
    ),
    Fallback(
        "Agent Runtime",
        (
            "services/investigation-service/tests/test_investigations.py"
            "::test_an_unreachable_runtime_or_a_lapsed_delegation",
            f"{RUNTIME}/test_agent_runtime.py"
            "::test_a_crashed_workers_run_is_taken_over_after_its_last_completed_step",
        ),
    ),
    Fallback(
        "Weather Service",
        (
            "services/production-impact-service/tests/test_windops_production_impact.py"
            "::test_without_a_start_or_a_fresh_forecast_the_observed_wind_is_held_and_it_says_why",
            "services/policy-service/tests/test_rules.py"
            "::test_a_missing_unavailable_stale_or_wrong_input_fails_closed",
            "mcp-servers/weather-mcp/tests/test_weather_mcp.py"
            "::test_an_unavailable_forecast_has_no_values",
        ),
    ),
    Fallback(
        "External Intelligence",
        (
            "services/external-intelligence-service/tests/test_external_intelligence_service.py"
            "::test_turned_off_over_quota_or_unreachable_is_a_recorded_data_gap",
        ),
    ),
    Fallback(
        "Production Impact",
        (f"{POLICY}::test_without_a_usable_estimate_cost_and_downtime_escalate",),
    ),
    Fallback(
        "Maintenance",
        (f"{APPROVAL}::test_a_decision_fails_closed_when_it_cannot_be_rechecked",),
    ),
    Fallback(
        "Inventory",
        (f"{MAINTENANCE}::test_each_part_is_checked_against_inventory",),
    ),
    Fallback(
        "Evaluation",
        (
            "services/evaluation-service/tests/test_live.py"
            "::test_a_service_that_cannot_be_reached_is_a_failed_check_never_a_skipped_one",
        ),
        note="Nothing in an investigation calls the Evaluation Service.",
    ),
    Fallback(
        "Embedding provider",
        (
            f"{KNOWLEDGE}/test_knowledge_service.py::test_an_outage_pauses_indexing_and_a_refusal_ends_it",
            f"{KNOWLEDGE}/test_embeddings.py::test_no_connection_counts_against_the_breaker_until_it_opens",
        ),
    ),
    Fallback(
        "Email provider",
        (
            "services/notification-service/tests/test_windops_notification.py"
            "::test_an_email_outage_never_holds_up_the_app_and_fails_after_five_tries",
        ),
    ),
    Fallback(
        "PostgreSQL",
        (
            "services/audit-service/tests/test_windops_audit.py"
            "::test_a_database_outage_makes_the_event_wait",
            f"{OUTBOX}::test_a_database_outage_does_not_stop_the_relay",
        ),
    ),
    Fallback(
        "Redis",
        (
            f"{CACHE}::test_cache_acts_empty_when_redis_is_down",
            f"{CACHE}::test_rate_limit_fails_closed_when_redis_is_down",
            f"{CACHE}::test_redis_errors_open_the_breaker",
            "services/web-bff-service/tests/test_web_bff.py::test_limits_fail_closed_when_redis_is_down",
            "shared/mcp/tests/test_mcp_limits_and_audit.py::test_when_redis_is_down_nothing_is_let_through",
        ),
    ),
    Fallback(
        "JetStream",
        (
            f"{OUTBOX}::test_an_outage_keeps_the_event_for_later",
            f"{EVENTS}::test_publish_reports_an_outage_so_the_caller_can_keep_the_event",
            f"{EVENTS}::test_nats_errors_open_the_breaker",
        ),
    ),
)


def all_tests() -> list[str]:
    """Every test named above, once each, in order."""
    named = [*BREAKER_TESTS, *(test for entry in FALLBACKS for test in entry.tests)]
    return list(dict.fromkeys(named))
