"""Every channel finds a connection to a pod that has gone, and every server allows it to look.

Before this, a rollout of one service left its callers' connections to the old pod hanging until
the operating system gave up, minutes later: the Phase 8 gate's dry run found the Policy Service
unable to reach a restarted Investigation Service, and a run failing at step 11 (ADR 0027).
"""

from windops_grpc import tls


def test_every_channel_pings_and_gives_up_on_a_silent_connection() -> None:
    options = dict(tls.CHANNEL_OPTIONS)
    assert options["grpc.keepalive_time_ms"] <= 30_000
    assert options["grpc.keepalive_timeout_ms"] <= 10_000
    assert options["grpc.keepalive_permit_without_calls"] == 1


def test_servers_accept_those_pings_so_none_is_dropped_as_abuse() -> None:
    server = dict(tls.SERVER_OPTIONS)
    assert server["grpc.keepalive_permit_without_calls"] == 1
    assert (
        server["grpc.http2.min_recv_ping_interval_without_data_ms"]
        <= dict(tls.CHANNEL_OPTIONS)["grpc.keepalive_time_ms"]
    )
