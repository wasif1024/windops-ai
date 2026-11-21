"""The approval queue's API, through real HTTP requests, with a fake Approval Service (M10.2)."""

import grpc
from test_admin import FakeService, stamp
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.approval.v1 import approval_pb2 as apr
from windops_web_bff.api import PREFIX

APPROVAL = "apr_" + "a" * 20
SNAPSHOT = "evs_" + "c" * 20
DECISION = {"decision": "approve", "reason": "Fits the findings", "expected_proposal_version": 1,
            "expected_evidence_snapshot_id": SNAPSHOT}  # fmt: skip


def approval(status: str = "pending") -> apr.Approval:
    made = apr.Approval(
        approval_id=APPROVAL, work_order_id="wo_" + "b" * 20, proposal_version=1,
        case_id="case_" + "d" * 20, turbine_id="dogger-east-t01", evidence_snapshot_id=SNAPSHOT,
        status=status, submitted_by="u-planner", drafted_by="service:fault-investigator",
        drafted_by_agent="agent:maintenance-planner", submitted_at=stamp(1_767_225_600),
        requested_at=stamp(1_767_225_601),
    )  # fmt: skip
    if status == "approved":
        made.decision.CopyFrom(apr.Decision(
            outcome="approved", decided_by="u-1", decided_at=stamp(1_767_229_200),
            reason="Fits the findings", proposal_version=1, evidence_snapshot_id=SNAPSHOT,
            requesters=["u-planner"],
            policy=apr.PolicyContext(policy_result_id="pol_" + "e" * 20, policy_outcome="pass",
                                     case_status="pending_review", evidence_version=1,
                                     work_order_revision=4,
                                     approval_rules="separation-of-duties-1",
                                     permissions_version=2),
        ))  # fmt: skip
    return made


def test_a_manager_lists_the_queue_and_decides_as_themselves() -> None:
    service = FakeService({
        "ListApprovals": apr.ListApprovalsResponse(approvals=[approval()]),
        "DecideApproval": apr.DecideApprovalResponse(approval=approval("approved")),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        listed = await world.client.get(f"{PREFIX}/approvals?status=pending&status=maybe",
                                        headers=auth(token()))  # fmt: skip
        [shown] = listed.json()["approvals"]
        assert (shown["status"], shown["drafted_by_agent"], shown["decision"]) == (
            "pending",
            "agent:maintenance-planner",
            None,
        )
        assert list(service.calls[0].request.statuses) == ["pending"]
        decided = await world.client.post(
            f"{PREFIX}/approvals/{APPROVAL}/decision",
            headers={**auth(token()), "Idempotency-Key": "d-1"}, json=DECISION,
        )  # fmt: skip
        body = decided.json()
        assert body["status"] == "approved" and body["decision"]["requesters"] == ["u-planner"]
        assert body["decision"]["policy"]["approval_rules"] == "separation-of-duties-1"
        sent = service.calls[1]
        assert (sent.user_id, sent.retry, sent.request.idempotency_key) == ("u-1", False, "d-1")
        assert sent.request.expected_evidence_snapshot_id == SNAPSHOT

    with_bff(scenario, services={"approval": service})


def test_a_bad_decision_is_refused_before_the_service_is_asked() -> None:
    service = FakeService({})

    async def scenario(world: World) -> None:
        headers = {**auth(token()), "Idempotency-Key": "d-2"}
        bodies = ({**DECISION, "decision": "maybe"}, {**DECISION, "expected_proposal_version": 0},
                  {**DECISION, "expected_evidence_snapshot_id": "nope"})  # fmt: skip
        for body in bodies:
            refused = await world.client.post(f"{PREFIX}/approvals/{APPROVAL}/decision",
                                              headers=headers, json=body)  # fmt: skip
            assert refused.status_code == 422
        bad_id = await world.client.get(f"{PREFIX}/approvals/apr_x", headers=auth(token()))
        assert bad_id.status_code == 422
        assert service.calls == []

    with_bff(scenario, services={"approval": service})


def test_a_refusal_is_explained_by_its_code_never_the_services_words() -> None:
    """RBAC-004 and RBAC-006 refusals get a code the screen explains; anything else is the
    usual problem, and none carries the service's message."""
    cases = [
        ("You drafted this one (RBAC-004, RBAC-008)", 403, "requester"),
        ("Only your own change counts not (RBAC-006)", 403, "own_change"),
        ("You do not have permission to decide on work orders", 403, "forbidden"),
    ]
    for details, status, code in cases:
        service = FakeService(error=rpc_error(grpc.StatusCode.PERMISSION_DENIED, details))

        async def scenario(world: World, code: str = code, status: int = status,
                           details: str = details) -> None:  # fmt: skip
            answer = await world.client.post(
                f"{PREFIX}/approvals/{APPROVAL}/decision",
                headers={**auth(token()), "Idempotency-Key": "d-3"}, json=DECISION,
            )  # fmt: skip
            assert (answer.status_code, answer.json()["code"]) == (status, code)
            assert details not in answer.text

        with_bff(scenario, services={"approval": service})
    superseded = FakeService(error=rpc_error(grpc.StatusCode.FAILED_PRECONDITION, "evs_x moved"))

    async def conflict(world: World) -> None:
        answer = await world.client.post(
            f"{PREFIX}/approvals/{APPROVAL}/decision",
            headers={**auth(token()), "Idempotency-Key": "d-4"}, json=DECISION,
        )  # fmt: skip
        assert answer.status_code == 409 and "evs_x" not in answer.text

    with_bff(conflict, services={"approval": superseded})
