"""SRS 7.7 through Web BFF: each way a dependency can be down is a 503, never a guess (RES-005).

A dependency is down when its breaker is open, its bulkhead is full, its deadline passes or it
answers UNAVAILABLE. Each is injected here for the Approval, Identity and Tenant services.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
import pytest
from test_admin import KEY, MEMBER
from test_approval_routes import APPROVAL, DECISION
from test_own_branding import CHANGE
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops_resilience import BreakerOpenError, BulkheadFullError
from windops_web_bff.api import PREFIX

OUTAGES: dict[str, Callable[[str], Exception]] = {
    "breaker open": lambda name: BreakerOpenError(name),
    "bulkhead full": lambda name: BulkheadFullError(name),
    "deadline passed": lambda name: TimeoutError(),
    "unavailable": lambda name: rpc_error(grpc.StatusCode.UNAVAILABLE, f"{name} is down"),
}


@dataclass
class Down:
    """A dependency that is down in one way, recording what it was asked."""

    name: str
    outage: str
    methods: list[str] = field(default_factory=list)

    async def __call__(self, method: str, request: Any, identity: str, retry: bool = True) -> Any:
        self.methods.append(method)
        raise OUTAGES[self.outage](self.name)


def assert_unavailable(response: Any) -> None:
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "unavailable"


@pytest.mark.parametrize("outage", OUTAGES)
def test_approval_down_rejects_decisions(outage: str) -> None:
    """Approval: reject decisions, and the queue answers 503 rather than a partial list."""
    approval = Down("approval", outage)

    async def scenario(world: World) -> None:
        decided = await world.client.post(
            f"{PREFIX}/approvals/{APPROVAL}/decision",
            headers={**auth(token()), "Idempotency-Key": "d-1"},
            json=DECISION,
        )
        assert_unavailable(decided)
        assert_unavailable(await world.client.get(f"{PREFIX}/approvals", headers=auth(token())))
        assert "DecideApproval" in approval.methods

    with_bff(scenario, services={"approval": approval})


@pytest.mark.parametrize("outage", OUTAGES)
def test_identity_down_keeps_sessions_and_refuses_user_changes(outage: str) -> None:
    """Identity: a signed-in user still works from their signed token; user changes are refused."""
    identity = Down("identity", outage)

    async def scenario(world: World) -> None:
        me = await world.client.get(f"{PREFIX}/me", headers=auth(token()))
        assert me.status_code == 200 and me.json()["user_id"]
        assert identity.methods == []
        sessions = await world.client.get(
            f"{PREFIX}/admin/users/{MEMBER}/sessions", headers=auth(token())
        )
        assert_unavailable(sessions)
        disabled = await world.client.post(
            f"{PREFIX}/admin/users/{MEMBER}/disable", headers={**auth(token()), **KEY}
        )
        assert_unavailable(disabled)

    with_bff(scenario, services={"identity": identity})


@pytest.mark.parametrize("outage", OUTAGES)
def test_tenant_down_refuses_tenant_changes(outage: str) -> None:
    """Tenant: tenant changes are refused, and nothing is shown as the tenant's own."""
    tenant = Down("tenant", outage)

    async def scenario(world: World) -> None:
        assert_unavailable(await world.client.get(f"{PREFIX}/tenant", headers=auth(token())))
        changed = await world.client.put(
            f"{PREFIX}/admin/branding", json=CHANGE, headers={**auth(token()), **KEY}
        )
        assert_unavailable(changed)
        assert "UpdateBranding" in tenant.methods

    with_bff(scenario, services={"tenant": tenant})
