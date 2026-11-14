"""The alarm console's API, through real HTTP requests, with a fake Alarm Service (M5.6)."""

import grpc
from test_admin import FakeService, stamp
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.alarm.v1 import alarm_pb2 as pb
from windops_web_bff.api import PREFIX

ALARM = pb.Alarm(
    alarm_id="alm_1", turbine_id="dogger-east-t01", code="GBX_OIL_TEMP_HIGH",
    tag="gearbox_oil_temp_c", description="Gearbox oil temperature high",
    severity=pb.ALARM_SEVERITY_HIGH, severity_source="device", state=pb.ALARM_STATE_ACTIVE,
    source="windops-simulator", raised_at=stamp(1_767_225_600), repeat_count=2, version=1,
)  # fmt: skip


def acknowledged() -> pb.Alarm:
    alarm = pb.Alarm()
    alarm.CopyFrom(ALARM)
    alarm.state, alarm.acknowledged_by, alarm.version = pb.ALARM_STATE_ACKNOWLEDGED, "u-1", 2
    return alarm


def test_the_console_lists_standing_alarms_with_their_severity_and_its_source() -> None:
    alarms = FakeService({"ListAlarms": pb.ListAlarmsResponse(alarms=[ALARM])})

    async def scenario(world: World) -> None:
        answer = await world.client.get(
            f"{PREFIX}/alarms?state=active&state=shelved&turbine_id=dogger-east-t01",
            headers=auth(token()),
        )
        assert answer.status_code == 200
        (alarm,) = answer.json()["alarms"]
        assert (alarm["severity"], alarm["severity_source"], alarm["state"]) == (
            "high", "device", "active",
        )  # fmt: skip
        assert alarm["raised_at"] == "2026-01-01T00:00:00Z" and alarm["repeat_count"] == 2
        request = alarms.calls[0].request
        assert list(request.states) == [pb.ALARM_STATE_ACTIVE, pb.ALARM_STATE_SHELVED]
        assert request.turbine_id == "dogger-east-t01"
        assert alarms.calls[0].user_id == "u-1", "asked as the signed-in user"

    with_bff(scenario, services={"alarm": alarms})


def test_one_alarm_shows_every_change_and_who_made_it() -> None:
    history = [
        pb.AlarmTransition(at=stamp(1_767_225_600), to_state=pb.ALARM_STATE_ACTIVE,
                           actor_type="device", actor_id="dev_1"),
        pb.AlarmTransition(at=stamp(1_767_225_660), from_state=pb.ALARM_STATE_ACTIVE,
                           to_state=pb.ALARM_STATE_ACKNOWLEDGED, actor_type="user",
                           actor_id="u-1", note="Crew informed"),
    ]  # fmt: skip
    alarms = FakeService({"GetAlarm": pb.GetAlarmResponse(alarm=ALARM, transitions=history)})

    async def scenario(world: World) -> None:
        body = (await world.client.get(f"{PREFIX}/alarms/alm_1", headers=auth(token()))).json()
        assert [(t["from_state"], t["to_state"], t["actor_type"]) for t in body["transitions"]] == [
            ("", "active", "device"), ("active", "acknowledged", "user"),
        ]  # fmt: skip
        assert body["transitions"][1]["note"] == "Crew informed"

    with_bff(scenario, services={"alarm": alarms})


def test_acknowledging_and_shelving_pass_the_key_and_the_reason_to_the_alarm_service() -> None:
    alarms = FakeService({
        "AcknowledgeAlarm": pb.AcknowledgeAlarmResponse(alarm=acknowledged()),
        "ShelveAlarm": pb.ShelveAlarmResponse(alarm=acknowledged()),
        "UnshelveAlarm": pb.UnshelveAlarmResponse(alarm=acknowledged()),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        headers = {**auth(token()), "Idempotency-Key": "ack-1"}
        done = await world.client.post(f"{PREFIX}/alarms/alm_1/acknowledge", headers=headers,
                                       json={"comment": "Crew informed"})  # fmt: skip
        assert done.json()["state"] == "acknowledged"
        ack = alarms.calls[0].request
        assert (ack.alarm_id, ack.comment, ack.idempotency_key) == ("alm_1", "Crew informed",
                                                                     "ack-1")  # fmt: skip
        await world.client.post(f"{PREFIX}/alarms/alm_1/shelve", headers=headers,
                                json={"reason": "Sensor work", "minutes": 30})  # fmt: skip
        assert (alarms.calls[1].request.reason, alarms.calls[1].request.minutes) == (
            "Sensor work", 30,
        )  # fmt: skip
        # A day at most, and a reason, before the Alarm Service is even asked.
        for body in ({"reason": "x", "minutes": 1441}, {"reason": "", "minutes": 5}):
            refused = await world.client.post(f"{PREFIX}/alarms/alm_1/shelve", headers=headers,
                                              json=body)  # fmt: skip
            assert refused.status_code == 422
        await world.client.post(f"{PREFIX}/alarms/alm_1/unshelve", headers=headers)
        assert len(alarms.calls) == 3

    with_bff(scenario, services={"alarm": alarms})


def test_an_alarm_the_user_may_not_read_is_not_found_and_a_settled_one_conflicts() -> None:
    hidden = FakeService(error=rpc_error(grpc.StatusCode.NOT_FOUND, "No such alarm"))
    settled = FakeService(error=rpc_error(grpc.StatusCode.FAILED_PRECONDITION, "It is cleared"))

    async def missing(world: World) -> None:
        answer = await world.client.get(f"{PREFIX}/alarms/alm_1", headers=auth(token()))
        assert answer.status_code == 404

    async def conflict(world: World) -> None:
        headers = {**auth(token()), "Idempotency-Key": "ack-2"}
        answer = await world.client.post(f"{PREFIX}/alarms/alm_1/acknowledge", headers=headers,
                                         json={})  # fmt: skip
        assert answer.status_code == 409

    with_bff(missing, services={"alarm": hidden})
    with_bff(conflict, services={"alarm": settled})
