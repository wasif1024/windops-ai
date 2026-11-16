"""The synthetic documents agree with the services that hold the same facts (SIM-007, M7.1).

The simulator's own test holds the documents to its physics, alarms and CMS. This one holds them
to the Asset Service's demo fleet, the Policy Service's climbing limits and Production Impact's
power curves, so an agent citing a document never contradicts the platform's own data.
"""

from windops_asset.seed import FARMS, MANUFACTURER, OFFSHORE, ONSHORE
from windops_policy.defaults import DEFAULT_RULES, FORECAST_AGE_SECONDS
from windops_production_impact.defaults import DEFAULT_REFERENCES
from windops_simulator import documents

DOCUMENTS = documents.load()
BY_ID = {d.document_id: d for d in documents.current(DOCUMENTS)}
MODELS = {OFFSHORE.model: OFFSHORE, ONSHORE.model: ONSHORE}
FARM_TENANT = {f.wind_farm_id: f.tenant_id for f in FARMS}
TENANT_MODELS = {
    tenant: {(OFFSHORE if f.offshore else ONSHORE).model for f in FARMS if f.tenant_id == tenant}
    for tenant in FARM_TENANT.values()
}


def prose(document: documents.Document) -> str:
    return " ".join(document.text().split())


def test_documents_apply_to_the_demo_fleets_models_and_wind_farms_only() -> None:
    for document in DOCUMENTS:
        assert set(document.applies_to) <= set(MODELS) | set(FARM_TENANT), document.path
    assert {m for d in DOCUMENTS for m in d.applies_to if m in MODELS} == set(MODELS)
    oem = [d for d in DOCUMENTS if d.path.startswith("oem/")]
    assert oem and all(d.issued_by.startswith(MANUFACTURER) for d in oem)


def test_a_document_goes_only_to_tenants_it_applies_to() -> None:
    for document in DOCUMENTS:
        for tenant in document.tenants:
            farms = {a for a in document.applies_to if a in FARM_TENANT}
            models = {a for a in document.applies_to if a in MODELS}
            if farms:
                assert {FARM_TENANT[f] for f in farms} == {tenant}, document.path
            else:
                assert models & TENANT_MODELS[tenant], f"{document.path} is not for {tenant}"


def test_each_manual_gives_its_models_hub_height_from_the_fleet() -> None:
    for document_id, model in (("WS-OM-8167", OFFSHORE), ("WS-OM-4514", ONSHORE)):
        manual = BY_ID[document_id]
        assert manual.applies_to == (model.model,)
        assert f"| Hub height | {model.hub_height_m:g} m above" in manual.text()
        assert f"| Rated power | {model.rated_power_kw:,.0f} kW |" in manual.text()


def test_the_safety_procedures_weather_limits_are_policys() -> None:
    limits = {rule_id: d["parameters"] for rule_id, _, _, d in DEFAULT_RULES}
    safety = BY_ID["WS-SAF-001"]
    text = safety.text()
    assert (f"| Mean wind speed at hub height | "
            f"{limits['work-at-height-wind']['above']:g} m/s |") in text  # fmt: skip
    assert f"| Gusts | {limits['work-at-height-gusts']['above']:g} m/s |" in text
    hours = FORECAST_AGE_SECONDS // 3600
    assert limits["work-at-height-wind"]["max_age_seconds"] == FORECAST_AGE_SECONDS
    assert f"A forecast older than {hours} hours is stale" in prose(safety)
    assert "Missing weather is never a reason to proceed" in prose(safety)


def test_production_impacts_power_curves_are_for_the_same_models() -> None:
    curves = {d["model"]["model"] for _, _, d in DEFAULT_REFERENCES if d["kind"] == "power_curve"}
    assert curves == set(MODELS)
