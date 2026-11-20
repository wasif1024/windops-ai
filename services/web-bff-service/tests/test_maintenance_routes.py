"""The maintenance planner and parts lookup's API, through real HTTP requests, with fake
Maintenance, Inventory, Asset and Investigation services (M9.4)."""

import grpc
from test_admin import FakeService, stamp
from test_web_bff import World, auth, rpc_error, token, with_bff
from windops.asset.v1 import asset_pb2
from windops.inventory.v1 import inventory_pb2 as inv
from windops.investigation.v1 import investigation_pb2
from windops.maintenance.v1 import maintenance_pb2 as mnt
from windops_web_bff.api import PREFIX

ORDER = "wo_" + "a" * 20
CASE = "case_" + "b" * 20
PLAN = {
    "title": "Oil cooler inspection", "action": "inspection", "summary": "Inspect the fan.",
    "tasks": [{"description": "Inspect the fan", "duration_hours": 3, "crew_size": 2,
               "requires_climb": True}],
    "parts": [{"part_number": "GBX-FAN/CAP.25", "quantity": 2}],
    "procedures": ["kn:GBX-OIL-01@3#12"], "planned_start": "2026-12-01T08:00:00Z",
    "planned_end": "2026-12-01T14:00:00Z", "estimated_downtime_hours": 6,
}  # fmt: skip


def work_order(status: str = "draft", revision: int = 1) -> mnt.WorkOrder:
    return mnt.WorkOrder(
        work_order_id=ORDER, version=1, revision=revision, case_id=CASE,
        turbine_id="dogger-east-t01", status=status, drafted_by="u-1",
        drafted_by_agent="agent:maintenance-planner", created_at=stamp(1_767_225_600),
        plan=mnt.Plan(title="Oil cooler inspection", action="inspection",
                      parts=[mnt.PartLine(part_number="GBX-FAN/CAP.25", quantity=2)],
                      planned_start=stamp(1_767_225_600)),
        validation=mnt.Validation(
            passed=False, policy_outcome="escalate", turbine_status="operating",
            problems=[mnt.Problem(field="parts[0]", code="part_short", message="1 of 2",
                                  blocking=False)],
            parts=[mnt.PartAvailability(part_number="GBX-FAN/CAP.25", quantity=2,
                                        status="short", available=1)],
        ),
    )  # fmt: skip


def test_a_planner_drafts_edits_validates_and_submits_as_themselves() -> None:
    maintenance = FakeService({
        "CreateDraft": mnt.CreateDraftResponse(work_order=work_order()),
        "UpdateDraft": mnt.UpdateDraftResponse(work_order=work_order(revision=2)),
        "ValidateWorkOrder": mnt.ValidateWorkOrderResponse(work_order=work_order(revision=2)),
        "SubmitWorkOrder": mnt.SubmitWorkOrderResponse(work_order=work_order("submitted", 3)),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        headers = {**auth(token()), "Idempotency-Key": "draft-1"}
        made = await world.client.post(f"{PREFIX}/work-orders", headers=headers,
                                       json={"case_id": CASE, "plan": PLAN})  # fmt: skip
        assert made.status_code == 201
        body = made.json()
        assert body["drafted_by_agent"] == "agent:maintenance-planner"
        assert body["validation"]["parts"] == [{"part_number": "GBX-FAN/CAP.25", "quantity": 2,
                                                "status": "short", "available": 1}]  # fmt: skip
        sent = maintenance.calls[0].request
        assert (sent.case_id, sent.idempotency_key, sent.plan.tasks[0].crew_size) == (
            CASE, "draft-1", 2,
        )  # fmt: skip
        assert sent.plan.planned_start.ToJsonString() == "2026-12-01T08:00:00Z"
        assert maintenance.calls[0].user_id == "u-1", "asked as the signed-in user"
        changed = await world.client.put(f"{PREFIX}/work-orders/{ORDER}/plan", headers=headers,
                                         json={"expected_revision": 1, "plan": PLAN})  # fmt: skip
        assert changed.json()["revision"] == 2
        assert maintenance.calls[1].request.expected_revision == 1
        checked = await world.client.post(f"{PREFIX}/work-orders/{ORDER}/validate",
                                          headers=auth(token()))  # fmt: skip
        assert checked.status_code == 200
        submitted = await world.client.post(
            f"{PREFIX}/work-orders/{ORDER}/submit", headers={**headers, "Idempotency-Key": "s-1"},
            json={"expected_revision": 2},
        )  # fmt: skip
        assert submitted.json()["status"] == "submitted"
        assert maintenance.calls[3].request.idempotency_key == "s-1"

    with_bff(scenario, services={"maintenance": maintenance})


def test_a_bad_plan_or_id_is_refused_before_any_service_is_asked() -> None:
    maintenance = FakeService({})

    async def scenario(world: World) -> None:
        headers = {**auth(token()), "Idempotency-Key": "draft-2"}
        for plan in ({**PLAN, "action": "teleport"}, {**PLAN, "tasks": [{**PLAN["tasks"][0],  # type: ignore[index]
                                                                          "crew_size": 99}]},
                     {**PLAN, "planned_start": "2026-12-01T08:00:00"}):  # fmt: skip
            refused = await world.client.post(f"{PREFIX}/work-orders", headers=headers,
                                              json={"case_id": CASE, "plan": plan})  # fmt: skip
            assert refused.status_code == 422
        missing = await world.client.get(f"{PREFIX}/work-orders/not-an-id", headers=auth(token()))
        assert missing.status_code == 422
        assert maintenance.calls == []

    with_bff(scenario, services={"maintenance": maintenance})


def test_an_agent_drafts_history_and_a_refused_submission_are_shown_as_they_are() -> None:
    history = [mnt.WorkOrderEvent(at=stamp(1_767_225_600), action="drafted", actor_type="agent",
                                  actor_id="service:fault-investigator",
                                  agent="agent:maintenance-planner", revision=1)]  # fmt: skip
    shown = FakeService({"GetWorkOrder": mnt.GetWorkOrderResponse(work_order=work_order(),
                                                                  history=history)})  # fmt: skip
    blocked = FakeService(error=rpc_error(grpc.StatusCode.FAILED_PRECONDITION, "It is blocked"))

    async def detail(world: World) -> None:
        body = (await world.client.get(f"{PREFIX}/work-orders/{ORDER}",
                                       headers=auth(token()))).json()  # fmt: skip
        assert [(e["action"], e["agent"]) for e in body["history"]] == [
            ("drafted", "agent:maintenance-planner")
        ]

    async def refused(world: World) -> None:
        answer = await world.client.post(
            f"{PREFIX}/work-orders/{ORDER}/submit",
            headers={**auth(token()), "Idempotency-Key": "s-2"}, json={"expected_revision": 1},
        )  # fmt: skip
        assert answer.status_code == 409

    with_bff(detail, services={"maintenance": shown})
    with_bff(refused, services={"maintenance": blocked})


def test_parts_for_a_turbine_fit_its_model_with_its_stores_stock() -> None:
    asset = FakeService({"GetTurbine": asset_pb2.GetTurbineResponse(turbine=asset_pb2.Turbine(
        turbine_id="dogger-east-t01", wind_farm_id="dogger-east",
        model=asset_pb2.TurbineModel(manufacturer="WindOps Synthetic", model="WS-8.0-167"),
    ))})  # fmt: skip
    model = inv.ModelRef(manufacturer="WindOps Synthetic", model="WS-8.0-167")
    part = inv.Part(part_number="GBX-FAN/CAP.25", name="Fan capacitor", unit="each",
                    component_kind="gearbox", version=1, fits=[model])  # fmt: skip
    inventory = FakeService({
        "ListParts": inv.ListPartsResponse(parts=[inv.ListedPart(
            part=part, stock=inv.StockLevel(part_number=part.part_number,
                                            wind_farm_id="dogger-east", on_hand=4, reserved=1,
                                            available=3))]),
        "GetPart": inv.GetPartResponse(part=part),
        "GetStock": inv.GetStockResponse(stock=[inv.StockLevel(
            part_number=part.part_number, wind_farm_id="dogger-east", on_hand=4, reserved=1,
            available=3)]),
    })  # fmt: skip

    async def scenario(world: World) -> None:
        found = (await world.client.get(f"{PREFIX}/parts?turbine_id=dogger-east-t01&query=fan",
                                        headers=auth(token()))).json()  # fmt: skip
        assert (found["wind_farm_id"], found["model"]["model"]) == ("dogger-east", "WS-8.0-167")
        assert found["parts"][0]["stock"]["available"] == 3
        asked = inventory.calls[0].request
        assert (asked.fits.model, asked.wind_farm_id, asked.query) == (
            "WS-8.0-167", "dogger-east", "fan",
        )  # fmt: skip
        words = (await world.client.get(f"{PREFIX}/parts?query=fan",
                                        headers=auth(token()))).json()  # fmt: skip
        assert words["model"] is None and not inventory.calls[1].request.HasField("fits")
        # A part number may hold "/" and ".", so it is a query, not a path.
        detail = await world.client.get(f"{PREFIX}/parts/detail?part_number=GBX-FAN/CAP.25",
                                         headers=auth(token()))  # fmt: skip
        assert detail.json()["stock"][0]["wind_farm_id"] == "dogger-east"

    with_bff(scenario, services={"asset": asset, "inventory": inventory})


def test_a_draft_is_made_for_an_active_case_the_user_may_read() -> None:
    cases = FakeService({"ListCases": investigation_pb2.ListCasesResponse(cases=[
        investigation_pb2.Case(case_id=CASE, turbine_id="dogger-east-t01",
                               title="Gearbox oil hot", status="investigating", severity="high"),
    ])})  # fmt: skip

    async def scenario(world: World) -> None:
        listed = (await world.client.get(f"{PREFIX}/work-orders/cases",
                                         headers=auth(token()))).json()  # fmt: skip
        assert [(c["case_id"], c["status"]) for c in listed] == [(CASE, "investigating")]
        assert list(cases.calls[0].request.statuses) == [], "the active ones, by default"

    with_bff(scenario, services={"investigation": cases})
