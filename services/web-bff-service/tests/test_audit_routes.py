"""The audit explorer and the notification rules through real HTTP requests, with fake Audit and
Notification services (M11.6)."""

from test_admin import FakeService, stamp
from test_web_bff import World, auth, token, with_bff
from windops.audit.v1 import audit_pb2
from windops.notification.v1 import notification_pb2 as ntf
from windops_web_bff.api import LONG_CALLS, PREFIX

RECORD = audit_pb2.AuditRecord(
    sequence=42, event_id="e-42", occurred_at=stamp(1_767_225_600),
    recorded_at=stamp(1_767_225_601), producer="knowledge", action="knowledge_document.held",
    actor_type="service", actor_id="knowledge", resource_type="knowledge_document",
    resource_id="WS-1@1", outcome="succeeded", correlation_id="c-9", previous_hash="a" * 64,
    record_hash="b" * 64,
)  # fmt: skip


def test_the_trail_is_explored_newest_first_with_every_filter() -> None:
    audit = FakeService({
        "ListAuditRecords": audit_pb2.ListAuditRecordsResponse(records=[RECORD],
                                                               next_page_token="41"),
        "VerifyAuditChain": audit_pb2.AuditChainVerification(valid=True, records_checked=42),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        headers = auth(token())
        page = await world.client.get(f"{PREFIX}/audit/records", headers=headers, params={
            "action_prefix": "knowledge_document.", "actor_id": "knowledge",
            "resource_id": "WS-1@1", "outcome": "succeeded", "correlation_id": "c-9",
            "from_time": "2026-01-01T00:00:00Z", "to_time": "2026-01-02T00:00:00Z",
            "page_token": "50"})  # fmt: skip
        body = page.json()
        assert body["next_page_token"] == "41" and body["records"][0]["resource_id"] == "WS-1@1"
        sent = audit.calls[-1].request
        assert sent.newest_first, "newest first unless asked otherwise"
        assert (sent.action_prefix, sent.actor_id, sent.outcome, sent.page_token) == (
            "knowledge_document.", "knowledge", "succeeded", "50")  # fmt: skip
        assert sent.to_time.ToSeconds() - sent.from_time.ToSeconds() == 86_400
        oldest = await world.client.get(f"{PREFIX}/audit/records?newest_first=false",
                                        headers=headers)  # fmt: skip
        assert oldest.status_code == 200 and not audit.calls[-1].request.newest_first
        check = (await world.client.get(f"{PREFIX}/audit/verify", headers=headers)).json()
        assert check == {"valid": True, "records_checked": 42, "first_invalid_sequence": 0}
        for bad in ("outcome=maybe", "actor_id=a%20b", "page_token=x1", "page_size=500"):
            refused = await world.client.get(f"{PREFIX}/audit/records?{bad}", headers=headers)
            assert refused.status_code == 422, bad
        assert all(c.user_id == "u-1" for c in audit.calls), "as the signed-in user"

    with_bff(scenario, services={"audit": audit})


def test_deliveries_and_rules_are_read_and_rules_set_as_the_person() -> None:
    rules = [ntf.Rule(kind="case_approved", audience="holders:maintenance.submit",
                      channels=["in_app"])]  # fmt: skip
    notification = FakeService({
        "ListDeliveries": ntf.ListDeliveriesResponse(deliveries=[ntf.DeliveryRecord(
            notification_id="n-1", kind="approval_requested", user_id="u-9", case_id="case_1",
            delivery=ntf.Delivery(channel="email", status="recorded", provider="record",
                                  attempts=1, last_error="smtp said no"))]),
        "GetNotificationRules": ntf.GetNotificationRulesResponse(rules=rules, customized=True),
        "SetNotificationRules": ntf.SetNotificationRulesResponse(rules=rules, customized=True),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        headers = auth(token())
        got = await world.client.get(f"{PREFIX}/audit/deliveries?case_id=case_1&status=recorded",
                                     headers=headers)  # fmt: skip
        [delivery] = got.json()
        assert (delivery["user_id"], delivery["status"]) == ("u-9", "recorded")
        assert "smtp" not in got.text, "a sender's error stays in the service"
        read = (await world.client.get(f"{PREFIX}/notifications/rules", headers=headers)).json()
        assert read["customized"] and read["rules"][0]["channels"] == ["in_app"]
        change = {**headers, "Idempotency-Key": "rules-change-1"}
        put = await world.client.put(f"{PREFIX}/notifications/rules", headers=change,
                                     json={"rules": [{"kind": "case_approved",
                                                      "audience": "holders:maintenance.submit",
                                                      "channels": ["in_app"]}]})  # fmt: skip
        assert put.status_code == 200
        sent = notification.calls[-1]
        assert (sent.request.idempotency_key, sent.retry) == ("rules-change-1", False)
        wrongs = [
            ("anything", "requester", ["in_app"]),
            ("case_approved", "everyone", ["in_app"]),
            ("case_approved", "requester", ["sms"]),
            ("case_approved", "requester", []),
        ]
        for kind, audience, channels in wrongs:
            wrong = {"kind": kind, "audience": audience, "channels": channels}
            refused = await world.client.put(f"{PREFIX}/notifications/rules", headers=change,
                                             json={"rules": [wrong]})  # fmt: skip
            assert refused.status_code == 422, wrong

    with_bff(scenario, services={"notification": notification})


def test_checking_the_chain_may_take_longer_than_an_ordinary_read() -> None:
    assert "VerifyAuditChain" in LONG_CALLS
