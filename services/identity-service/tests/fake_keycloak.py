"""A small, in-memory Keycloak admin API, for the calls keycloak.py is allowed to make."""

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

REALM = "windops"


@dataclass
class FakeKeycloak:
    organizations: dict[str, str] = field(default_factory=dict)  # alias -> id
    members: dict[str, set[str]] = field(default_factory=dict)  # organization id -> users
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    credentials: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    passwords: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_with: int | None = None
    fail_adding_members: bool = False

    def add_tenant(self, alias: str) -> str:
        organization = str(uuid.uuid4())
        self.organizations[alias] = organization
        self.members[organization] = set()
        return organization

    def add_user(self, alias: str, email: str, *, user_id: str | None = None) -> str:
        user = user_id or str(uuid.uuid4())
        self.users[user] = {
            "id": user,
            "email": email,
            "enabled": True,
            "createdTimestamp": 1_767_225_600_000,
            "requiredActions": [],
        }
        self.members[self.organizations[alias]].add(user)
        return user

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # As sent, so a test can see that an ID was escaped and not decoded into the path.
        self.calls.append((request.method, request.url.raw_path.decode().split("?")[0]))
        if path == f"/realms/{REALM}/protocol/openid-connect/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 300})
        if self.fail_with is not None:
            return httpx.Response(self.fail_with)
        if request.headers.get("Authorization") != "Bearer token":
            return httpx.Response(401)
        route = path.removeprefix(f"/admin/realms/{REALM}/")
        return self._route(request.method, route, request)

    def _route(self, method: str, route: str, request: httpx.Request) -> httpx.Response:
        if (method, route) == ("GET", "organizations"):
            return httpx.Response(
                200, json=[{"id": i, "alias": a} for a, i in self.organizations.items()]
            )
        if found := re.fullmatch(r"organizations/([^/]+)/members/([^/]+)", route):
            organization, user = found.groups()
            known = user in self.members.get(organization, set())
            return httpx.Response(200, json=self.users[user]) if known else httpx.Response(404)
        if found := re.fullmatch(r"organizations/([^/]+)/members", route):
            organization = found.group(1)
            if method == "POST":
                if self.fail_adding_members:
                    return httpx.Response(400)
                self.members[organization].add(json.loads(request.content))
                return httpx.Response(201)
            first = int(request.url.params.get("first", 0))
            maximum = int(request.url.params.get("max", 100))
            ordered = sorted(self.members[organization])
            return httpx.Response(
                200, json=[self.users[u] for u in ordered][first : first + maximum]
            )
        if route == "users":
            if method == "POST":
                body = json.loads(request.content)
                user = str(uuid.uuid4())
                self.users[user] = {"id": user, "createdTimestamp": 1_767_225_600_000, **body}
                return httpx.Response(201, headers={"Location": f"/users/{user}"})
            email = request.url.params["email"]
            return httpx.Response(200, json=[u for u in self.users.values() if u["email"] == email])
        if found := re.fullmatch(r"users/([^/]+)(/[a-z-]+)?", route):
            user, action = found.group(1), found.group(2) or ""
            if user not in self.users:
                return httpx.Response(404)
            return self._user_route(method, user, action, request)
        return httpx.Response(404)

    def _user_route(
        self, method: str, user: str, action: str, request: httpx.Request
    ) -> httpx.Response:
        if (method, action) == ("GET", ""):
            return httpx.Response(200, json=self.users[user])
        if (method, action) == ("PUT", ""):
            self.users[user].update(json.loads(request.content))
            return httpx.Response(204)
        if (method, action) == ("DELETE", ""):
            del self.users[user]
            for members in self.members.values():
                members.discard(user)
            return httpx.Response(204)
        if (method, action) == ("PUT", "/reset-password"):
            self.passwords[user] = json.loads(request.content)
            return httpx.Response(204)
        if (method, action) == ("GET", "/sessions"):
            return httpx.Response(200, json=self.sessions.get(user, []))
        if (method, action) == ("POST", "/logout"):
            self.sessions[user] = []
            return httpx.Response(204)
        if (method, action) == ("GET", "/credentials"):
            return httpx.Response(200, json=self.credentials.get(user, []))
        return httpx.Response(405)
