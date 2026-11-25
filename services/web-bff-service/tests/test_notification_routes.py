"""The user's own notifications, through real HTTP requests, with a fake service (M10.5)."""

from test_admin import FakeService, stamp
from test_web_bff import World, auth, token, with_bff
from windops.notification.v1 import notification_pb2 as ntf
from windops_web_bff.api import PREFIX


def test_a_person_reads_their_own_notifications_and_marks_them_read() -> None:
    service = FakeService({
        "ListMyNotifications": ntf.ListMyNotificationsResponse(
            notifications=[ntf.Notification(
                notification_id="ntf_" + "a" * 20, kind="approval_requested",
                title="Work order wo_1 v1 awaits approval", body="Submitted for case case_1.",
                link="/screens/approvals", case_id="case_1", work_order_id="wo_1",
                created_at=stamp(1_767_225_600),
                deliveries=[ntf.Delivery(channel="in_app", status="delivered", attempts=1),
                            ntf.Delivery(channel="email", status="recorded", attempts=1)],
            )],
            unread_count=1,
        ),
        "MarkNotificationsRead": ntf.MarkNotificationsReadResponse(marked=1),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        listed = await world.client.get(f"{PREFIX}/notifications?unread_only=true",
                                        headers=auth(token()))  # fmt: skip
        body = listed.json()
        assert body["unread_count"] == 1
        [shown] = body["notifications"]
        assert (shown["link"], shown["read_at"]) == ("/screens/approvals", None)
        assert [(d["channel"], d["status"]) for d in shown["deliveries"]] == [
            ("in_app", "delivered"), ("email", "recorded")]  # fmt: skip
        first = service.calls[0]
        assert (first.user_id, first.request.unread_only) == ("u-1", True), "as the user"
        marked = await world.client.post(
            f"{PREFIX}/notifications/read",
            headers=auth(token()),
            json={"notification_ids": ["ntf_" + "a" * 20]},
        )
        assert marked.json() == {"marked": 1}
        assert list(service.calls[1].request.notification_ids) == ["ntf_" + "a" * 20]
        unsigned = await world.client.get(f"{PREFIX}/notifications")
        assert unsigned.status_code == 401

    with_bff(scenario, services={"notification": service})
