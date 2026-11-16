"""The synthetic document set: each document's metadata as its manifest records it, every page
marked synthetic, and every figure the same as the simulator's own (SIM-006, SIM-007)."""

import re
from datetime import date
from importlib import resources

import pytest
from windops_simulator import alarms, cms, documents, faults, models
from windops_simulator.fleet import DEMO_TURBINES

DOCUMENTS = documents.load()
BY_PATH = {d.path: d for d in DOCUMENTS}
DEMO_TENANTS = {t.tenant_id for t in DEMO_TURBINES}
# Real turbine makers, whose names no synthetic document may carry.
REAL_MAKERS = ("Siemens", "Gamesa", "Vestas", "General Electric", "GE Vernova", "Nordex", "Enercon",
               "Goldwind", "Mingyang", "Envision", "Suzlon", "Senvion", "Acciona")  # fmt: skip
CITED = re.compile(r"\b(?:WS|NS|AW|ADV)-[A-Z]{2,}(?:-[A-Z0-9]+)*")


def body(document: documents.Document) -> str:
    return document.text()


def table(text: str, heading: str) -> list[list[str]]:
    """The rows of the first table under a heading, each as its cells."""
    section = text.split(heading, 1)[1]
    rows = []
    for line in section.splitlines()[1:]:
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif rows:
            break
    return rows[2:]


def test_the_manifest_lists_every_document_and_only_those() -> None:
    root = resources.files(documents.__name__)
    on_disk = {
        f"{folder.name}/{file.name}"
        for folder in root.iterdir() if folder.is_dir() and not folder.name.startswith("_")
        for file in folder.iterdir() if file.name.endswith(".md")
    }  # fmt: skip
    assert on_disk == set(BY_PATH)
    assert len({(d.document_id, d.revision) for d in DOCUMENTS}) == len(DOCUMENTS)
    assert documents.set_version() == "synthetic-documents-1"


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda d: d.path)
def test_a_documents_header_says_what_the_manifest_records(document: documents.Document) -> None:
    header = documents.read_header(body(document))
    supersedes = (f"{document.supersedes.document_id} revision {document.supersedes.revision}"
                  if document.supersedes else None)  # fmt: skip
    assert header == {
        "Title": document.title, "Document": document.document_id,
        "Revision": str(document.revision), "Type": document.type,
        "Published": document.published, "Issued by": document.issued_by,
        "Applies to": ", ".join(document.applies_to), "Classification": document.classification,
        **({"Supersedes": supersedes} if supersedes else {}),
    }  # fmt: skip
    assert document.type in documents.TYPES
    assert document.classification in documents.CLASSIFICATIONS
    assert document.purpose in documents.PURPOSES
    assert date.fromisoformat(document.published) <= date(2026, 11, 16)


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda d: d.path)
def test_every_document_says_it_is_synthetic_and_names_no_real_maker(
    document: documents.Document,
) -> None:
    text = body(document)
    banner = text.split("\n\n")[2]
    assert banner.startswith(documents.BANNER) and "must not be used" in banner
    assert "synthetic" in document.issued_by.lower() or "fictional" in document.issued_by.lower()
    assert not [maker for maker in REAL_MAKERS if maker.lower() in text.lower()]


def test_a_later_revision_supersedes_an_earlier_one_and_only_the_newest_is_current() -> None:
    for document in DOCUMENTS:
        if document.supersedes:
            earlier = next(d for d in DOCUMENTS if (d.document_id, d.revision) == (
                document.supersedes.document_id, document.supersedes.revision))  # fmt: skip
            assert earlier.document_id == document.document_id
            assert earlier.revision < document.revision and earlier.published < document.published
    current = documents.current(DOCUMENTS)
    assert len({d.document_id for d in current}) == len(current)
    assert ("WS-PRC-GBX-012", 2) not in {(d.document_id, d.revision) for d in current}


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda d: d.path)
def test_every_document_a_document_cites_is_in_the_set(document: documents.Document) -> None:
    known = {d.document_id for d in DOCUMENTS}
    assert set(CITED.findall(body(document))) <= known


def test_who_each_document_is_for() -> None:
    for document in DOCUMENTS:
        assert set(document.tenants) <= DEMO_TENANTS, document.path
        if document.purpose == "evaluation":
            assert document.tenants == (), "evaluation material is never loaded by default"
        else:
            assert document.tenants, document.path
        if document.classification == "restricted":
            assert len(document.tenants) == 1, "a site's own procedure is its tenant's alone"
    demo = {d.classification for d in DOCUMENTS if d.purpose == "demo"}
    assert demo == set(documents.CLASSIFICATIONS), "every classification, for permission tests"


def test_the_alarm_reference_is_the_controllers_own_rules() -> None:
    text = body(BY_PATH["oem/ws-ref-alm-r1.md"])
    rows = {row[0].strip("`"): row[1:] for row in table(text, "## 2. Alarms")}

    def level(value: float, reading: str) -> str:
        return f"{value:.1f} mm/s" if reading.endswith("_mm_s") else f"{value:g} °C"

    expected = {t.code: [level(t.raise_at, t.reading), level(t.clear_below, t.reading), t.severity]
                for t in alarms.THRESHOLDS}  # fmt: skip
    expected[alarms.SENSOR_LOST] = [
        f"{alarms.RAISE_AFTER.seconds} seconds with no signal",
        "a signal again",
        "low",
    ]
    assert rows == expected
    prose = " ".join(text.split())
    assert alarms.RULES_VERSION in prose
    assert f"held for {alarms.RAISE_AFTER.seconds} seconds" in prose
    assert f"below its clear level for {alarms.CLEAR_AFTER.seconds} seconds" in prose
    for code in alarms.CODES:
        assert f"### `{code}`" in text


@pytest.mark.parametrize(("path", "rated_kw", "rotor_m"),
                         [("oem/ws-om-8167-r4.md", 8000.0, 167.0),
                          ("oem/ws-om-4514-r2.md", 4500.0, 145.0)])  # fmt: skip
def test_a_manuals_technical_data_is_the_simulators_physics(
    path: str, rated_kw: float, rotor_m: float
) -> None:
    assert any((t.rated_power_kw, t.rotor_diameter_m) == (rated_kw, rotor_m) for t in DEMO_TURBINES)
    design = models.Design(rated_kw, rotor_m)
    data = dict(row[:2] for row in table(body(BY_PATH[path]), "## 2. Technical data"))
    assert data["Rated power"] == f"{rated_kw:,.0f} kW"
    assert data["Rotor diameter"] == f"{rotor_m:g} m"
    assert data["Cut-in wind speed"] == f"{models.CUT_IN_MPS:g} m/s"
    assert data["Cut-out wind speed"].startswith(f"{models.CUT_OUT_MPS:g} m/s")
    assert data["Rated wind speed"] == f"{design.rated_wind_mps:.1f} m/s"
    assert data["Maximum rotor speed"] == (f"{design.maximum_rotor_rpm:.1f} rpm, a tip speed of "
                                           f"{models.MAXIMUM_TIP_SPEED_MPS:g} m/s")  # fmt: skip
    assert data["Gearbox"] == f"Three stages, ratio {models.GEARBOX_RATIO:g}:1"
    pinion = next(s.gear_teeth for s in cms.SENSORS if s.gear_teeth)
    assert data["High-speed pinion"] == f"{pinion} teeth"
    generator_rpm = design.maximum_rotor_rpm * models.GEARBOX_RATIO
    assert f"{generator_rpm:,.0f} rpm at maximum rotor speed" in data["Generator"]


def test_the_manuals_alarm_levels_are_the_controllers() -> None:
    rows = table(body(BY_PATH["oem/ws-om-8167-r4.md"]), "## 4. Normal operating ranges")
    raised = {t.code: t.raise_at for t in alarms.THRESHOLDS}
    for _, _, alarm in rows:
        code, level = re.fullmatch(r"`(\w+)` at ([\d.]+) .+", alarm).groups()  # type: ignore[union-attr]
        assert raised[code] == float(level)
    assert len(rows) == len(alarms.THRESHOLDS) - 1, "every reading but the critical oil level"


def test_the_cms_guide_is_the_simulators_kinematics() -> None:
    text = body(BY_PATH["oem/ws-eng-cms-001-r1.md"])
    rows = {row[0].strip("`"): row for row in table(text, "## 2. The drivetrain's kinematics")}
    assert set(rows) == {s.name for s in cms.SENSORS}
    for sensor in cms.SENSORS:
        _, shaft, bearing, mesh, resonance = rows[sensor.name]
        assert shaft == ("Rotor" if sensor.shaft == "rotor" else "High-speed shaft")
        assert bearing == f"{sensor.bearing_order:g} orders, outer race"
        assert mesh == (f"{sensor.gear_teeth} orders, the {sensor.gear_teeth}-tooth pinion"
                        if sensor.gear_teeth else "None")  # fmt: skip
        assert resonance == f"About {sensor.resonance_hz:,.0f} Hz"
    prose = " ".join(text.split())
    assert f"{cms.WINDOW_SAMPLES:,} values at {cms.SAMPLE_RATE_HZ:,} Hz" in prose
    assert f"{models.GEARBOX_RATIO:g} times faster" in prose


def test_every_fault_and_alarm_has_a_current_document_for_it() -> None:
    labels = {label for _, label in faults.PROFILES.values()}
    known = labels | set(alarms.CODES)
    covered = {c for d in documents.current(DOCUMENTS) if d.purpose == "demo" for c in d.covers}
    assert {c for d in DOCUMENTS for c in d.covers} <= known
    assert covered == known


def test_the_evaluation_document_carries_a_prompt_injection() -> None:
    evaluation = [d for d in DOCUMENTS if d.purpose == "evaluation"]
    assert evaluation
    for document in evaluation:
        assert "ignore your previous instructions" in body(document).lower()
