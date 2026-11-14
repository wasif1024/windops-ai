"""Keep the security diagram true to the tracker, so it cannot quietly go stale."""

import re
from pathlib import Path

from scripts.generate.readme_visuals import (
    AGENT_SERVICE,
    APPLICATION_GROUPS,
    ENTRY_SERVICES,
    OVERVIEW_GROUPS,
    all_controls,
    is_running,
    read_phase_progress,
)
from scripts.setup.provision_postgres import SERVICE_NAMESPACES

ROOT = Path(__file__).resolve().parent.parent
TRACKER = (ROOT / "docs" / "phases" / "README.md").read_text()


def module_state(module: str) -> str | None:
    found = re.search(rf"^- \[([ x])\] \*\*{re.escape(module)} ", TRACKER, re.M)
    return found.group(1) if found else None


def planned_modules() -> list[tuple[str, str]]:
    return [
        (control.text, module.strip())
        for control in all_controls()
        if control.module
        for module in control.module.split(",")
    ]


def test_every_cited_module_exists_in_the_tracker() -> None:
    for text, module in planned_modules():
        assert module_state(module) is not None, f"{text}: {module} is not in the tracker"


def test_a_control_is_not_shown_as_planned_once_its_modules_are_done() -> None:
    """When this fails, mark the control as in place and run the visuals script."""
    by_control: dict[str, list[str | None]] = {}
    for text, module in planned_modules():
        by_control.setdefault(text, []).append(module_state(module))
    for text, states in by_control.items():
        assert any(state == " " for state in states), f"{text} is finished, so update the diagram"


def test_roadmap_reads_all_phases_with_the_tracker_totals() -> None:
    phases = read_phase_progress()
    expected = [str(n) for n in range(1, 16)]
    expected.insert(4, "4A")  # Added after Phase 4 by ADR 0006.
    assert [number for number, _, _, _ in phases] == expected
    table = dict(re.findall(r"^\| (\d+[A-Z]?) \| \[[^\]]+\]\([^)]*\) \| (\d+) \|", TRACKER, re.M))
    for number, _, _, total in phases:
        assert int(table[number]) == total


def test_roadmap_progress_counts_ticked_modules(tmp_path: Path) -> None:
    tracker = tmp_path / "README.md"
    tracker.write_text("## Phase 1: First\n\n- [x] a\n- [ ] b\n\n## Phase 2: Second\n\n- [ ] c\n")
    assert read_phase_progress(tracker) == [("1", "First", 1, 2), ("2", "Second", 0, 1)]


def test_readme_shows_one_architecture_diagram_and_the_animation() -> None:
    """The README is the overview: the Kubernetes view and the agents' animation, nothing more."""
    readme = (ROOT / "README.md").read_text()
    images = re.findall(r"docs/assets/[\w-]+\.(?:png|gif)", readme)
    assert sorted(set(images)) == [
        "docs/assets/agent-orchestration.gif", "docs/assets/kubernetes-on-azure.png",
    ]  # fmt: skip
    # The architecture comes before the agents, and the detailed diagrams are a link away.
    assert readme.index("kubernetes-on-azure.png") < readme.index("agent-orchestration.gif")
    assert "(docs/architecture.md)" in readme


def test_the_architecture_document_has_the_detailed_diagrams() -> None:
    document = (ROOT / "docs" / "architecture.md").read_text()
    for image in ("assets/platform-architecture.png", "assets/azure-architecture.png"):
        assert f"]({image})" in document, image


def test_the_kubernetes_view_draws_every_domain_service_as_a_pod() -> None:
    from scripts.generate.kubernetes_view import BACK_END

    drawn = [name for _, _, members in APPLICATION_GROUPS for name, _ in members]
    assert list(BACK_END) == drawn


def test_the_azure_diagram_marks_exactly_what_the_pipeline_deploys() -> None:
    from scripts.generate.readme_visuals import AKS_NAMESPACES
    from scripts.setup.azure_platform import WORKLOADS

    deployed = {name for _, workloads in AKS_NAMESPACES for _, name, now in workloads if now}
    assert deployed == {*WORKLOADS, "windops-postgres-provision", "windops-otel-collector"}


def test_the_azure_diagram_draws_the_pipeline_stages_in_order() -> None:
    from scripts.generate.readme_visuals import PIPELINE_STAGES

    pipeline = (ROOT / "infrastructure" / "azure" / "pipelines" / "azure-pipelines.yml").read_text()
    stages = [name for name, _, _ in PIPELINE_STAGES if name != "Approval"]
    # "Remove compute" is the stage RemoveCompute.
    ids = ["".join(word[0].upper() + word[1:] for word in name.split()) for name in stages]
    found = [pipeline.index(f"- stage: {stage}") for stage in ids]
    assert found == sorted(found)


def test_a_service_runs_once_it_has_an_image_or_a_deployment() -> None:
    assert is_running("Asset")
    assert is_running("API Gateway")
    assert not is_running("Agent Runtime")


def test_end_to_end_diagram_draws_every_srs_service_once() -> None:
    srs = (ROOT / "docs" / "srs.md").read_text()
    table = srs[srs.index("### 7.2 Services") : srs.index("### 7.3")]
    names = re.findall(r"^\| ([A-Z][^|]+?) \|", table, re.M)
    expected = sorted(name.removesuffix(" (new)") for name in names if name != "Service")
    drawn = [*ENTRY_SERVICES, AGENT_SERVICE]
    drawn += [name for _, _, members in APPLICATION_GROUPS for name, _ in members]
    assert sorted(drawn) == expected


def test_readme_shows_the_console_as_it_runs() -> None:
    """Four screens of the real console, each a file in the repository, after the diagrams."""
    readme = (ROOT / "README.md").read_text()
    screens = re.findall(r"docs/assets/screens/[\w-]+\.png", readme)
    assert sorted(set(screens)) == [
        "docs/assets/screens/alarm-console.png", "docs/assets/screens/alarm-detail.png",
        "docs/assets/screens/fleet-overview.png", "docs/assets/screens/turbine-detail.png",
    ]  # fmt: skip
    # The scaffold check runs the tests in a copy without the assets, so check files where they are.
    if (ROOT / "docs/assets").is_dir():
        assert all((ROOT / screen).is_file() for screen in screens)
    assert readme.index("agent-orchestration.gif") < readme.index("## The console today")


def test_readme_shows_the_industry_images() -> None:
    readme = (ROOT / "README.md").read_text()
    for image in (
        "docs/assets/industry/offshore-wind-farm.jpg",
        "docs/assets/industry/turbine-technician.jpg",
        "docs/assets/industry/control-room.jpg",
    ):
        assert image in readme, image


def test_readme_does_not_let_the_control_room_image_make_claims() -> None:
    """It shows an ISO label and uptime figures, which this project cannot claim."""
    readme = (ROOT / "README.md").read_text()
    assert "not claims about this project" in readme
    assert "holds no certifications" in readme


def test_the_diagram_draws_every_domain_service_and_its_database() -> None:
    drawn = [name for _, _, members in OVERVIEW_GROUPS for name, _ in members]
    assert len(drawn) == 20
    assert len(SERVICE_NAMESPACES) == 20


def test_the_kubernetes_view_marks_what_the_pipeline_deploys_as_on_azure() -> None:
    from scripts.generate.kubernetes_view import BACK_END, ON_AZURE, PLANNED, status_of

    on_azure = {name for name in BACK_END if status_of(name) == ON_AZURE}
    assert on_azure == {"Asset", "Audit"}  # Authorization and Tenant are drawn in the front end
    assert status_of("Authorization") == status_of("Tenant") == ON_AZURE
    assert status_of("Agent Runtime") == PLANNED
