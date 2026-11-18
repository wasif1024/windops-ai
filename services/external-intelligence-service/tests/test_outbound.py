"""What goes to the provider: the caller's words, made generic (EXT-006)."""

import pytest
from windops_external_intelligence import outbound

FLEET = outbound.Identifiers(
    tenant=("north-sea", "North Sea Wind Ltd"),
    sites=("dogger-east", "Dogger East", "hornsea-west", "Hornsea West"),
    turbines=("dogger-east-t01", "Dogger East T01", "SYN-DOGGER-EAST-T01", "WS-8.0-167"),
)
SITE = outbound.Site("GB", 54.82, 2.07, offshore=True)


def test_the_tenants_sites_turbines_and_records_are_taken_out_and_only_named_by_kind() -> None:
    sent = outbound.compose(
        "Gearbox oil overheating on Dogger East T01 (SYN-DOGGER-EAST-T01, a WS-8.0-167) at "
        "dogger-east for North Sea Wind Ltd, case case_0123456789abcdef0123, ask ops@example.com "
        "or see https://intranet.example.com/x",
        FLEET,
    )
    for kept_out in ("Dogger", "SYN-", "WS-8.0", "North Sea Wind", "case_", "ops@", "intranet"):
        assert kept_out.lower() not in sent.query.lower(), kept_out
    assert "Gearbox oil overheating" in sent.query
    assert set(sent.redactions) == {"turbine", "site", "tenant", "record_id", "email", "address"}


def test_a_hyphenated_id_is_taken_out_as_words_too_and_regardless_of_case() -> None:
    sent = outbound.compose("storm warnings near DOGGER EAST and hornsea west", FLEET)
    assert "dogger" not in sent.query.lower() and "hornsea" not in sent.query.lower()
    assert sent.redactions == ("site",)


def test_a_name_inside_another_word_is_left_alone() -> None:
    sent = outbound.compose("gearbox bearing failures in modern turbines", FLEET)
    assert sent.query == "gearbox bearing failures in modern turbines" and sent.redactions == ()


def test_a_site_goes_as_its_region_and_a_turbine_as_its_class() -> None:
    region = outbound.region(SITE)
    assert region == "offshore, United Kingdom, near 55°N 2°E"
    assert (
        outbound.region(outbound.Site("ES", 42.13, -3.71, offshore=False))
        == "onshore, Spain, near 42°N 4°W"
    )
    turbine = outbound.turbine_class(8000.0, offshore=True)
    assert turbine == "8 MW offshore wind turbine"
    sent = outbound.compose(
        "severe weather warnings", FLEET, region_text=region, class_text=turbine
    )
    assert sent.query == f"severe weather warnings {turbine} {region}"


@pytest.mark.parametrize(
    ("query", "says"),
    [
        ("", "needed"),
        ("x" * 401, "at most"),
        ("why is the key " + "tvly-" + "dev-" + "a" * 30 + " refused", "an API key"),
        ("Dogger East T01 at dogger-east", "Nothing generic"),
    ],
)
def test_a_query_that_cannot_go_out_is_refused(query: str, says: str) -> None:
    with pytest.raises(outbound.QueryRefusedError, match=says):
        outbound.compose(query, FLEET)


def test_what_goes_out_is_at_most_the_limit() -> None:
    sent = outbound.compose("wind " * 79, FLEET, region_text="offshore, " + "x" * 100)
    assert len(sent.query) <= outbound.MAXIMUM_QUERY
