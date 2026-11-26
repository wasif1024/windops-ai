"""People review an investigation's findings: accept, reject, challenge or annotate (FR-INV-006)."""

import json
import time

import grpc
from test_decisions import pending_review
from test_investigations import Setup, case_of
from test_windops_investigation import SECRET, TENANT, World, as_user, code_of
from windops.investigation.v1 import investigation_pb2 as pb
from windops_auth import VerifiedIdentity, sign_identity
from windops_investigation import findings

REPORT = {
    "case_id": "c", "observed_facts": [{"statement": "Oil at 85 °C", "citations": ["evs_1"]}],
    "hypotheses": [{"statement": "A stuck thermostatic valve", "support": ["evs_1"],
                    "against": [], "confidence": 0.6}],
    "data_gaps": ["No oil sample"], "recommended_actions": [{"action": "inspection"}],
    "confidence": 0.6, "explanation": "The oil runs hot under load",
}  # fmt: skip


def review(case_id: str, finding: str, action: str, note: str = "",
           key: str = "") -> pb.ReviewFindingRequest:  # fmt: skip
    return pb.ReviewFindingRequest(case_id=case_id, finding=finding, action=action, note=note,
                                   idempotency_key=key)  # fmt: skip


def test_each_section_of_the_report_is_reviewable_by_where_it_is() -> None:
    found = findings.findings_of(json.dumps(REPORT))
    assert set(found) == {"observed_facts[0]", "hypotheses[0]", "data_gaps[0]",
                          "recommended_actions[0]"}  # fmt: skip
    findings.check(json.dumps(REPORT), "hypotheses[0]", "accept", "")
    for finding, action, note in (("hypotheses[1]", "accept", ""), ("explanation", "accept", ""),
                                  ("hypotheses[0]", "challenge", ""),
                                  ("hypotheses[0]", "approve", "x")):  # fmt: skip
        try:
            findings.check(json.dumps(REPORT), finding, action, note)
        except ValueError:
            continue
        raise AssertionError(f"{finding} {action} was let through")


def test_a_reviewer_challenges_a_finding_and_it_is_kept_beside_the_report() -> None:
    setup = Setup()

    async def scenario(world: World) -> None:
        case_id, _, run_id = await pending_review(world, setup)
        assert await code_of(world.stub.ReviewFinding(
            review(case_id, "hypotheses[0]", "accept"), metadata=as_user("u-engineer"), timeout=5
        )) == grpc.StatusCode.FAILED_PRECONDITION, "no report yet"  # fmt: skip
        setup.runtime.runs[run_id].result_json = json.dumps(REPORT)

        got = await world.stub.GetInvestigationReport(
            pb.GetInvestigationReportRequest(case_id=case_id), metadata=as_user("u-operator"),
            timeout=5,
        )  # fmt: skip
        assert got.run_id == run_id and json.loads(got.report_json) == REPORT and not got.reviews

        for who, code in (("u-operator", grpc.StatusCode.PERMISSION_DENIED),
                          ("u-auditor", grpc.StatusCode.PERMISSION_DENIED)):  # fmt: skip
            assert await code_of(world.stub.ReviewFinding(
                review(case_id, "hypotheses[0]", "accept"), metadata=as_user(who), timeout=5
            )) == code, f"{who} may read, not review"  # fmt: skip
        agent = VerifiedIdentity("u-engineer", TENANT, user_id="u-engineer",
                                 auth_time=int(time.time()), actor="agent:supervisor")  # fmt: skip
        assert await code_of(world.stub.ReviewFinding(
            review(case_id, "hypotheses[0]", "accept"), timeout=5,
            metadata=(("x-windops-identity", sign_identity(agent, SECRET)),),
        )) == grpc.StatusCode.PERMISSION_DENIED, "never an agent"  # fmt: skip
        assert await code_of(world.stub.ReviewFinding(
            review(case_id, "hypotheses[3]", "accept"), metadata=as_user("u-engineer"), timeout=5
        )) == grpc.StatusCode.NOT_FOUND  # fmt: skip
        assert await code_of(world.stub.ReviewFinding(
            review(case_id, "hypotheses[0]", "challenge"), metadata=as_user("u-engineer"),
            timeout=5,
        )) == grpc.StatusCode.INVALID_ARGUMENT, "a challenge says why"  # fmt: skip

        challenge = review(case_id, "hypotheses[0]", "challenge",
                           "The valve was replaced last month", key="c-1")  # fmt: skip
        made = await world.stub.ReviewFinding(challenge, metadata=as_user("u-engineer"), timeout=5)
        again = await world.stub.ReviewFinding(challenge, metadata=as_user("u-engineer"), timeout=5)
        assert again == made, "a retry answers the same (GRP-007)"
        await world.stub.ReviewFinding(review(case_id, "observed_facts[0]", "accept"),
                                       metadata=as_user("u-engineer"), timeout=5)  # fmt: skip
        got = await world.stub.GetInvestigationReport(
            pb.GetInvestigationReportRequest(case_id=case_id), metadata=as_user("u-operator"),
            timeout=5,
        )  # fmt: skip
        assert [(r.finding, r.action, r.reviewed_by) for r in got.reviews] == [
            ("hypotheses[0]", "challenge", "u-engineer"),
            ("observed_facts[0]", "accept", "u-engineer"),
        ]
        case = await case_of(world, case_id)
        assert [h.action for h in case.history][-2:] == ["finding_challenged", "finding_accepted"]
        assert case.case.status == "pending_review", "a review decides nothing"
        updated = (await world.events("case.updated"))[-1]
        assert updated.action == "finding_accepted"

    setup.run(scenario, runtime=setup.runtime, secret=SECRET)
