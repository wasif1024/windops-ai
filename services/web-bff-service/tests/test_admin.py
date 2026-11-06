"""The administrator console's API, through real HTTP requests with fake services behind it."""

import uuid
from dataclasses import dataclass, field
from typing import Any

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.audit.v1 import audit_pb2
from windops.authorization.v1 import authorization_pb2 as authz
from windops.identity.v1 import identity_pb2
from windops_auth import STEP_UP_MESSAGE
from windops_grpc import verify_identity
from windops_web_bff.api import PREFIX

SECRET = "x" * 32
# A fresh value each run, as the browser sends.
KEY = {"Idempotency-Key": uuid.uuid4().hex}
MEMBER = "6d3a1f2e-8c4b-4e1a-9f7d-2b5c8e0a1d34"


@dataclass
class Recorded:
    method: str
    request: Any
    user_id: str | None
    retry: bool


@dataclass
class FakeService:
    """Answers each method with a set message, recording what each call carried."""

    answers: dict[str, Any] = field(default_factory=dict)
    calls: list[Recorded] = field(default_factory=list)
    error: grpc.aio.AioRpcError | None = None

    async def __call__(self, method: str, request: Any, identity: str, retry: bool = True) -> Any:
        self.calls.append(
            Recorded(method, request, verify_identity(identity, SECRET).user_id, retry)
        )
        if self.error is not None:
            raise self.error
        return self.answers[method]


def stamp(seconds: int) -> Timestamp:
    value = Timestamp()
    value.FromSeconds(seconds)
    return value


USER = identity_pb2.User(
    member_id=MEMBER,
    email="planner@north-sea.example",
    first_name="Pat",
    last_name="Lee",
    enabled=True,
    created_at=stamp(1_767_225_600),
)
GRANT = authz.RoleGrant(
    grant_id="g-1",
    tenant_id="north-sea",
    user_id=MEMBER,
    role="maintenance-planner",
    scope=authz.Scope(wind_farm_id="farm-1"),
    granted_by="u-1",
    granted_at=stamp(1_767_225_600),
)


def run(services: dict[str, FakeService], scenario: Any) -> None:
    with_bff(scenario, services=services)


def test_users_are_listed_as_the_signed_in_administrator() -> None:
    identity = FakeService(
        {"ListUsers": identity_pb2.ListUsersResponse(users=[USER], next_page_token="next")}
    )

    async def scenario(world: World) -> None:
        response = await world.client.get(
            f"{PREFIX}/admin/users", params={"page_size": 20}, headers=auth(token())
        )
        assert response.status_code == 200
        assert response.json() == {
            "users": [
                {
                    "member_id": MEMBER,
                    "email": "planner@north-sea.example",
                    "first_name": "Pat",
                    "last_name": "Lee",
                    "enabled": True,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
            "next_page_token": "next",
        }
        (call,) = identity.calls
        # The tenant is never named in the request: it comes from the user's verified identity.
        assert (call.user_id, call.request.page_size, call.request.target_tenant_id) == (
            "u-1",
            20,
            "",
        )

    run({"identity": identity}, scenario)


def test_a_new_users_password_is_shown_once_and_the_change_is_never_retried_here() -> None:
    identity = FakeService(
        {
            "CreateUser": identity_pb2.CreateUserResponse(
                user=USER, temporary_password="Wo1!temporary"
            )
        }
    )

    async def scenario(world: World) -> None:
        response = await world.client.post(
            f"{PREFIX}/admin/users",
            json={"email": "planner@north-sea.example", "first_name": "Pat"},
            headers={**auth(token()), **KEY},
        )
        assert response.status_code == 201
        assert response.json()["temporary_password"] == "Wo1!temporary"
        (call,) = identity.calls
        assert call.request.idempotency_key == KEY["Idempotency-Key"]
        assert call.retry is False

    run({"identity": identity}, scenario)


def test_a_repeated_create_has_no_password() -> None:
    identity = FakeService(
        {"CreateUser": identity_pb2.CreateUserResponse(user=USER, already_created=True)}
    )

    async def scenario(world: World) -> None:
        response = await world.client.post(
            f"{PREFIX}/admin/users",
            json={"email": "planner@north-sea.example"},
            headers={**auth(token()), **KEY},
        )
        body = response.json()
        assert (body["temporary_password"], body["already_created"]) == (None, True)

    run({"identity": identity}, scenario)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/admin/users"),
        ("post", f"/admin/users/{MEMBER}/disable"),
        ("post", f"/admin/users/{MEMBER}/reactivate"),
        ("post", f"/admin/users/{MEMBER}/sessions/revoke"),
        ("post", "/admin/role-grants"),
        ("post", "/admin/role-grants/g-1/revoke"),
        ("put", "/admin/roles/auditor/permissions"),
    ],
)
@pytest.mark.parametrize("key", [None, "", "has space", "x" * 65])
def test_every_change_needs_a_valid_idempotency_key(
    method: str, path: str, key: str | None
) -> None:
    """GRP-007: so the browser can resend a change it is unsure of, safely."""
    bodies = {
        "/admin/users": {"email": "a@b.example"},
        "/admin/role-grants": {"user_id": MEMBER, "role": "auditor"},
        "/admin/roles/auditor/permissions": {"permissions": []},
    }
    services = {"identity": FakeService(), "authorization": FakeService()}

    async def scenario(world: World) -> None:
        headers = auth(token()) if key is None else {**auth(token()), "Idempotency-Key": key}
        response = await world.client.request(
            method.upper(), f"{PREFIX}{path}", json=bodies.get(path), headers=headers
        )
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_request"
        assert response.headers["content-type"] == "application/problem+json"
        assert not services["identity"].calls and not services["authorization"].calls

    run(services, scenario)


@pytest.mark.parametrize(
    ("path", "method", "answer"),
    [
        (
            f"/admin/users/{MEMBER}/disable",
            "DisableUser",
            identity_pb2.DisableUserResponse(user=USER),
        ),
        (
            f"/admin/users/{MEMBER}/reactivate",
            "ReactivateUser",
            identity_pb2.ReactivateUserResponse(user=USER),
        ),
        (
            f"/admin/users/{MEMBER}/sessions/revoke",
            "RevokeSessions",
            identity_pb2.RevokeSessionsResponse(sessions_ended=2),
        ),
    ],
)
def test_user_changes_name_the_user_and_carry_the_key(path: str, method: str, answer: Any) -> None:
    identity = FakeService({method: answer})

    async def scenario(world: World) -> None:
        response = await world.client.post(f"{PREFIX}{path}", headers={**auth(token()), **KEY})
        assert response.status_code == 200
        (call,) = identity.calls
        assert (call.method, call.request.member_id, call.retry) == (method, MEMBER, False)
        assert call.request.idempotency_key == KEY["Idempotency-Key"]

    run({"identity": identity}, scenario)


def test_sessions_and_mfa_status_are_shown() -> None:
    session = identity_pb2.Session(
        session_id="s-1",
        ip_address="10.0.0.5",
        started_at=stamp(1_767_225_600),
        clients=["windops-web"],
    )
    identity = FakeService(
        {
            "ListSessions": identity_pb2.ListSessionsResponse(sessions=[session]),
            "GetMfaStatus": identity_pb2.GetMfaStatusResponse(otp_configured=True),
        }
    )

    async def scenario(world: World) -> None:
        sessions = await world.client.get(
            f"{PREFIX}/admin/users/{MEMBER}/sessions", headers=auth(token())
        )
        assert sessions.json()["sessions"] == [
            {
                "session_id": "s-1",
                "ip_address": "10.0.0.5",
                "started_at": "2026-01-01T00:00:00Z",
                "last_access_at": None,
                "clients": ["windops-web"],
            }
        ]
        mfa = await world.client.get(f"{PREFIX}/admin/users/{MEMBER}/mfa", headers=auth(token()))
        assert mfa.json() == {
            "otp_configured": True,
            "recovery_codes_configured": False,
            "setup_required": False,
        }

    run({"identity": identity}, scenario)


def test_a_bad_member_id_never_reaches_a_service() -> None:
    identity = FakeService()

    async def scenario(world: World) -> None:
        response = await world.client.get(
            f"{PREFIX}/admin/users/..%2Fadmin/mfa", headers=auth(token())
        )
        assert response.status_code in (404, 422)
        assert identity.calls == []


def test_a_bad_body_is_refused_without_repeating_it() -> None:
    identity = FakeService()

    async def scenario(world: World) -> None:
        response = await world.client.post(
            f"{PREFIX}/admin/users",
            json={"email": "<script>" * 40},
            headers={**auth(token()), **KEY},
        )
        assert response.status_code == 422
        assert "<script>" not in response.text
        assert identity.calls == []

    run({"identity": identity}, scenario)

    run({"identity": identity}, scenario)


def test_a_role_is_granted_in_a_scope_and_grants_are_listed() -> None:
    authorization = FakeService(
        {
            "GrantRole": authz.GrantRoleResponse(grant=GRANT),
            "ListRoleGrants": authz.ListRoleGrantsResponse(grants=[GRANT]),
        }
    )

    async def scenario(world: World) -> None:
        created = await world.client.post(
            f"{PREFIX}/admin/role-grants",
            json={"user_id": MEMBER, "role": "maintenance-planner", "wind_farm_id": "farm-1"},
            headers={**auth(token()), **KEY},
        )
        assert created.status_code == 201
        assert created.json()["wind_farm_id"] == "farm-1"
        assert created.json()["revoked_at"] is None
        listed = await world.client.get(
            f"{PREFIX}/admin/role-grants",
            params={"user_id": MEMBER, "include_revoked": "true"},
            headers=auth(token()),
        )
        assert [item["grant_id"] for item in listed.json()["grants"]] == ["g-1"]
        grant, listing = authorization.calls
        assert (grant.request.grantee_id, grant.request.scope.wind_farm_id, grant.retry) == (
            MEMBER,
            "farm-1",
            False,
        )
        assert (listing.request.grantee_id, listing.request.include_revoked) == (MEMBER, True)

    run({"authorization": authorization}, scenario)


def test_a_roles_permissions_are_read_and_replaced() -> None:
    mapping = authz.RolePermissions(
        tenant_id="north-sea", role="auditor", permissions=["audit.read"], customized=True
    )
    authorization = FakeService(
        {
            "GetRolePermissions": authz.GetRolePermissionsResponse(role_permissions=mapping),
            "SetRolePermissions": authz.SetRolePermissionsResponse(role_permissions=mapping),
        }
    )

    async def scenario(world: World) -> None:
        read = await world.client.get(
            f"{PREFIX}/admin/roles/auditor/permissions", headers=auth(token())
        )
        assert read.json() == {"role": "auditor", "permissions": ["audit.read"], "customized": True}
        replaced = await world.client.put(
            f"{PREFIX}/admin/roles/auditor/permissions",
            json={"permissions": ["audit.read"]},
            headers={**auth(token()), **KEY},
        )
        assert replaced.status_code == 200
        assert list(authorization.calls[1].request.permissions) == ["audit.read"]

    run({"authorization": authorization}, scenario)


def test_access_denials_come_from_the_audit_trail() -> None:
    record = audit_pb2.AuditRecord(
        sequence=7,
        occurred_at=stamp(1_767_225_600),
        action="authorization.denied",
        actor_id="u-2",
        resource_type="permission",
        resource_id="maintenance.approve",
        outcome="denied",
    )
    audit = FakeService({"ListAuditRecords": audit_pb2.ListAuditRecordsResponse(records=[record])})

    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/admin/access-denials", headers=auth(token()))
        assert response.json() == {
            "denials": [
                {
                    "sequence": 7,
                    "occurred_at": "2026-01-01T00:00:00Z",
                    "user_id": "u-2",
                    "permission": "maintenance.approve",
                }
            ],
            "next_page_token": "",
        }
        (call,) = audit.calls
        assert (call.request.action, call.user_id) == ("authorization.denied", "u-1")

    run({"audit": audit}, scenario)


def test_a_change_without_a_recent_second_factor_asks_for_one() -> None:
    """RFC 9470: the browser signs in again with a second factor, then resends."""
    identity = FakeService(error=rpc_error(grpc.StatusCode.PERMISSION_DENIED, STEP_UP_MESSAGE))

    async def scenario(world: World) -> None:
        response = await world.client.post(
            f"{PREFIX}/admin/users/{MEMBER}/disable", headers={**auth(token()), **KEY}
        )
        assert response.status_code == 401
        assert response.json()["code"] == "step_up_required"
        assert 'acr_values="mfa"' in response.headers["WWW-Authenticate"]

    run({"identity": identity}, scenario)


def test_someone_without_the_permission_is_refused_in_our_own_words() -> None:
    audit = FakeService(error=rpc_error(grpc.StatusCode.PERMISSION_DENIED, "internal detail"))

    async def scenario(world: World) -> None:
        response = await world.client.get(f"{PREFIX}/admin/access-denials", headers=auth(token()))
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"
        assert "internal detail" not in response.text

    run({"audit": audit}, scenario)
