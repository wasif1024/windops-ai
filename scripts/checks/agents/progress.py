#!/usr/bin/env python3
"""Prove live progress on the cluster: an investigation through the gateway, the case's ID at
once, its progress streamed and resumed, and the case left for review (M8.11, API-003).

A real login of the demo operator asks Web BFF to investigate a turbine. The check proves:

- the case's ID comes back at once, before any work, and a retry with the same key is the same
  case;
- Investigation starts the run itself once the evidence is frozen, under the operator's own
  delegation, rehearsed, so nothing is asked of Claude;
- the progress streams as server-sent events through the gateway, one ID each, in order; a
  stream dropped part way and resumed with Last-Event-ID misses nothing and repeats nothing;
- every step of the run is in it, then the run's end, then the case's move to pending_review
  (SRS 5.4 step 13), and the stream says it is finished;
- another tenant's operator cannot watch it.

Run `make services` and `make gateway-routes` first. Needs local port 9443 free.
"""

import json
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from typing import Any

import grpc
import httpx
import jwt

from scripts.checks.agents.runtime import sql
from scripts.checks.agents.tools import ENGINEER, TURBINE, Cases
from scripts.checks.fleet.asset import bootstrap, signing_key
from scripts.checks.identity.identity import Realm
from scripts.lib.settings import KUBE_CONTEXT
from scripts.setup.identity_seed import (
    PUBLIC_URL,
    demo_email,
    keycloak_public,
    read_demo_passwords,
)

API = PUBLIC_URL.removesuffix("/auth") + "/api/v1"
TENANT, OTHER = "north-sea", "atlas-wind"
STEPS = (
    "receive", "validate", "snapshot", "plan", "specialists", "documents", "research", "impact",
    "aggregate", "validate_output", "policy_checks", "recommend", "conclude",
)  # fmt: skip
# Events to read before the check drops its first stream.
DROP_AFTER = 4


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"Failed: {message}")
    print(f"ok - {message}", flush=True)


class Operator:
    """A demo operator, signed in through the gateway, signing in again when the token lapses."""

    def __init__(self, realm: Realm, tenant: str) -> None:
        self._realm, self._tenant = realm, tenant
        self.token = self._sign_in()
        self.user_id = str(jwt.decode(self.token, options={"verify_signature": False})["sub"])

    def _sign_in(self) -> str:
        tokens = self._realm.login(demo_email(self._tenant), read_demo_passwords()[self._tenant])
        if tokens is None:
            raise RuntimeError(f"{self._tenant}'s demo operator could not sign in")
        return tokens.access

    def fresh(self) -> str:
        expires = jwt.decode(self.token, options={"verify_signature": False})["exp"]
        if expires - time.time() < 60:
            self.token = self._sign_in()
        return self.token

    def headers(self, **more: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.fresh()}", **more}


def events(operator: Operator, case_id: str, after: int) -> Iterator[dict[str, Any]]:
    """One stream's server-sent events, as {"id", "event", "data"}; comments are skipped."""
    headers = operator.headers(**({"Last-Event-ID": str(after)} if after else {}))
    with httpx.stream("GET", f"{API}/investigations/{case_id}/events", headers=headers,
                      timeout=httpx.Timeout(30, read=40)) as response:  # fmt: skip
        if response.status_code != 200:
            raise RuntimeError(f"The stream answered {response.status_code}")
        if not response.headers["content-type"].startswith("text/event-stream"):
            raise RuntimeError("The stream is not text/event-stream")
        fields: dict[str, Any] = {}
        for line in response.iter_lines():
            if not line:
                if "event" in fields:
                    yield fields
                fields = {}
            elif not line.startswith(":"):
                name, _, value = line.partition(": ")
                fields[name] = json.loads(value) if name == "data" else value


def start(operator: Operator) -> str:
    key = f"progress-check-{uuid.uuid4().hex[:12]}"
    body = {"turbine_id": TURBINE, "title": "Progress check: gearbox noise", "severity": "medium"}

    def post() -> httpx.Response:
        headers = operator.headers(**{"Idempotency-Key": key})
        return httpx.post(f"{API}/investigations", json=body, headers=headers, timeout=15)

    began = time.monotonic()
    first = post()
    took = time.monotonic() - began
    if first.status_code != 202:
        raise RuntimeError(f"Investigating answered {first.status_code}: {first.text[:300]}")
    answer = first.json()
    check(
        first.status_code == 202 and answer["case_id"].startswith("case_")
        and answer["investigation_status"] == "waiting" and answer["run_id"] == "",
        f"The case's ID comes back at once ({took:.2f}s), its investigation waiting for evidence",
    )  # fmt: skip
    again = post().json()["case_id"]
    check(again == answer["case_id"], "A retry with the same key is the same case")
    return str(answer["case_id"])


def follow(operator: Operator, case_id: str) -> list[dict[str, Any]]:
    """Read the stream, drop it after a few events, then resume until it is finished."""
    seen: list[dict[str, Any]] = []
    connections, dropped = 0, False
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        connections += 1
        after = int(seen[-1]["id"]) if seen else 0
        for event in events(operator, case_id, after):
            if event["event"] == "finished":
                check(dropped, f"The stream says it is finished, over {connections} connections")
                return seen
            seen.append(event)
            if not dropped and len(seen) == DROP_AFTER:
                dropped = True
                print(f"  dropping the stream after event {event['id']}", flush=True)
                break
    raise RuntimeError("The investigation never finished")


def prove(operator: Operator, stranger: Operator, cases: Cases) -> None:
    case_id = start(operator)
    cases.opened.append(case_id)
    stream = f"{API}/investigations/{case_id}/events"
    refused = httpx.get(stream, headers=stranger.headers(), timeout=15)
    check(refused.status_code == 404, "Another tenant's operator cannot watch it")
    seen = follow(operator, case_id)
    ids = [int(e["id"]) for e in seen]
    check(ids == list(range(1, len(ids) + 1)),
          f"Its {len(ids)} events came once each, in order, across the dropped stream")  # fmt: skip
    kinds = [(e["event"], e["data"]["step"] or e["data"]["status"]) for e in seen]
    check(
        kinds == [
            ("requested", ""), ("started", ""), ("case_status", "investigating"),
            *[("step", name) for name in STEPS],
            ("run_ended", "completed"), ("case_status", "pending_review"),
        ],
        "It shows the request, the run's start, all 13 steps, the run's end and pending_review",
    )  # fmt: skip
    run_id = seen[1]["data"]["run_id"]
    case = httpx.get(f"{API}/investigations/{case_id}", headers=operator.headers(),
                     timeout=15).json()  # fmt: skip
    check(
        (case["status"], case["investigation_status"], case["run_id"])
        == ("pending_review", "completed", run_id),
        "The case waits for a person's review, its investigation completed (SRS 5.4 step 13)",
    )  # fmt: skip
    # The runtime's own ID, from its own stream.
    row = sql(
        "SELECT requested_by || '|' || status || '|' || (delegation_id <> '') || '|' || "  # noqa: S608
        f"parameters FROM runs WHERE run_id = '{run_id}'",
        tenant=TENANT,
    )
    requested_by, status, delegated, parameters = row.split("|", 3)
    check(
        (requested_by, status, delegated) == (f"user:{operator.user_id}", "completed", "true")
        and json.loads(parameters)["rehearse"] is True,
        "Investigation started the run under the operator's own delegation, rehearsed: no Claude",
    )  # fmt: skip


def main() -> int:
    try:
        key = signing_key()
        with keycloak_public() as public:
            realm = Realm(public)
            operator, stranger = Operator(realm, TENANT), Operator(realm, OTHER)
            # Who cancels the check's case at the end.
            bootstrap(TENANT, ENGINEER, "reliability-engineer")
            cases = Cases(key)
            try:
                prove(operator, stranger, cases)
            finally:
                cases.close()
    except (
        RuntimeError, OSError, KeyError, ValueError, grpc.RpcError, httpx.HTTPError,
        subprocess.CalledProcessError,
    ) as error:  # fmt: skip
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("Progress check passed")
    return 0


if __name__ == "__main__":
    assert KUBE_CONTEXT  # noqa: S101
    sys.exit(main())
