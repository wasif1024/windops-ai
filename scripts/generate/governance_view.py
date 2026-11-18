"""The AI governance crosswalk picture: four frameworks, and the WindOps controls that answer them.

Each control cites the tracker modules that deliver it, or "gap" when nothing in the plan does yet.
Its marker comes from the tracker when the picture is drawn: in place once every cited module is
done, planned until then. So the picture cannot claim more than the tracker does.
docs/ai-governance.md explains each row, and tests keep the two and the tracker matched.
"""

import re
from dataclasses import dataclass

from scripts.generate.readme_visuals import (
    AMBER,
    ASSETS,
    GREEN,
    LINE,
    MUTED,
    PANEL,
    RED,
    TEXT,
    TRACKER,
    Canvas,
    mix,
)

GAP = "gap"
IN_PLACE, PLANNED, PROPOSED = "in place", "planned", "proposed"


@dataclass(frozen=True)
class Answer:
    text: str
    # Tracker modules, comma separated, such as "M6.1", or GAP when nothing in the plan does it.
    module: str


@dataclass(frozen=True)
class Theme:
    name: str
    iso: str
    nist: str
    nca: str
    sdaia: str
    answers: tuple[Answer, ...]


# The references are to clause and control areas, paraphrased: the standards' text is not quoted.
THEMES = (
    Theme(
        "Governance and accountability",
        "Cl. 4 and 5, context and leadership; A.2 AI policy; A.3 internal organization",
        "GOVERN 1, 2, 4",
        "Governance: risk, people, awareness",
        "Accountability and responsibility",
        (
            Answer("Roles and permissions for each person, denied by default", "M3.5"),
            Answer("Append-only, hash-chained audit records", "M2.7"),
            Answer("Audit records name the agent and the person behind it", "M8.5"),
            Answer("AI policy, management system scope and named owners", "M14.6"),
        ),
    ),
    Theme(
        "Risk and impact assessment",
        "Cl. 6.1 risk assessment and treatment; A.5 AI system impact assessment",
        "MAP 1 to 5; MANAGE 1",
        "Risk management, reviewed periodically",
        "Humanity; social and environmental benefit",
        (
            Answer("Versioned policy rules block or escalate, and fail closed", "M6.1"),
            Answer("Threat model and security review", "M14.1"),
            Answer("AI risk register and system impact assessment", "M14.6"),
        ),
    ),
    Theme(
        "Human oversight",
        "A.9 use of AI systems",
        "MAP 3; MANAGE 2",
        "Identity and access",
        "Humanity",
        (
            Answer("Critical cases and low confidence go to a person", "M6.1"),
            Answer("Agents can only draft: never submit, reserve or approve", "M9.5"),
            Answer("A person decides, with separation of duties", "M10.2"),
        ),
    ),
    Theme(
        "Transparency and traceability",
        "A.6 AI system life cycle; A.8 information for interested parties",
        "GOVERN 1.6; MEASURE 2.8, 2.9",
        "Logging and monitoring",
        "Transparency and explainability",
        (
            Answer("Immutable, hashed evidence behind every case", "M5.5"),
            Answer("Policy results keep every rule version and input", "M6.1"),
            Answer("Each call follows a versioned Claude policy, metered per run", "M8.3"),
            Answer("Runs pin model, prompt and tool versions; results cite sources", "M8.8"),
            Answer("A published system card for each AI capability", "M14.6"),
        ),
    ),
    Theme(
        "Data quality and privacy",
        "A.7 data for AI systems",
        "MAP 2; MEASURE 2.10",
        "Data protection",
        "Privacy and security; the PDPL",
        (
            Answer("Quality labels on every sample; simulated data marked synthetic", "M4.6"),
            Answer("Row-level tenant isolation, attacked on every data path", "M3.12"),
            Answer("No secrets or needless personal data in traces", "M8.9"),
            Answer("PDPL personal-data map and cross-border transfer review", "M15.10"),
        ),
    ),
    Theme(
        "Security and resilience",
        "A.6 life cycle, with ISO/IEC 27001",
        "MEASURE 2.7",
        "Defense and resilience",
        "Privacy and security",
        (
            Answer("mTLS, MFA and step-up; CodeQL and secret scans", "M3.3, M3.2, M1.2"),
            Answer("Circuit breakers on every dependency; outages fail closed", "M1.4"),
            Answer(
                "Default-deny network policy, signed images, tested backups", "M13.3, M14.2, M14.3"
            ),
            Answer("Independent penetration test", "M14.1"),
        ),
    ),
    Theme(
        "Validity and evaluation",
        "Cl. 9 performance evaluation; A.6 verification and validation",
        "MEASURE 1 to 4",
        "—",
        "Reliability and safety",
        (
            Answer("Rules proven against the simulator's hidden ground truth", "M5.2"),
            Answer("Critical safety cases pass 100% of repeated runs", "M12.8"),
            Answer("Responsible AI dashboard", "M12.9"),
        ),
    ),
    Theme(
        "Third parties",
        "A.10 third-party and customer relationships",
        "GOVERN 6; MANAGE 3",
        "Third-party cybersecurity",
        "Accountability and responsibility",
        (
            Answer("Dependency audits and image scans in CI", "M1.2, M1.6"),
            Answer("Vendor checks: forecast provider, Claude's data handling", "M6.3, M8.1"),
            Answer("Per-call MCP tool tokens; untrusted content wrapped", "M7.5"),
            Answer("A tool allowlist for each agent", "M8.6"),
        ),
    ),
    Theme(
        "Monitoring and improvement",
        "Cl. 9.2 internal audit, 9.3 review; Cl. 10 improvement",
        "MANAGE 4; GOVERN 1.5",
        "Logging and monitoring",
        "Accountability and responsibility",
        (
            Answer("Dashboards and alerts for every service and data store", "M1.9"),
            Answer("AI incident response and reporting runbook", "M14.6"),
            Answer("Internal audit and management review cycle", "M14.6"),
        ),
    ),
    Theme(
        "Fairness and bias",
        "A.5 impacts on individuals and groups",
        "MEASURE 2.11",
        "—",
        "Fairness",
        (
            Answer("Decisions concern turbines, not people, and record their rules", "M6.1"),
            Answer("Limited applicability, recorded in the Statement of Applicability", "M14.6"),
        ),
    ),
)
FRAMEWORKS = (
    ("ISO/IEC 42001", "AI management system"),
    ("NIST AI RMF 1.0", "AI 100-1"),
    ("NCA AICG-1", "AI cybersecurity"),
    ("SDAIA", "AI ethics, PDPL"),
)


def done(module: str, tracker: str) -> bool:
    found = re.search(rf"^- \[([ x])\] \*\*{re.escape(module)} ", tracker, re.M)
    if found is None:
        raise ValueError(f"{module} is not in the tracker")
    return found.group(1) == "x"


def status(answer: Answer, tracker: str) -> str:
    if answer.module == GAP:
        return PROPOSED
    modules = [m.strip() for m in answer.module.split(",")]
    return IN_PLACE if all(done(m, tracker) for m in modules) else PLANNED


def marker(c: Canvas, x: float, y: float, state: str) -> None:
    if state == IN_PLACE:
        c.dot(x, y, 5, GREEN)
        return
    c.dot(x, y, 5, AMBER if state == PLANNED else RED)
    c.dot(x, y, 3, PANEL)


# Column edges: theme, the four frameworks, then the WindOps answers.
COLUMNS = (40, 250, 520, 690, 880, 1070, 1560)
CELL, ANSWER, LEADING = 12.5, 13.0, 16.5


def build_governance_view() -> None:
    tracker = TRACKER.read_text()
    c = Canvas(1600, 1700)
    c.text(40, 40, "WindOps AI and AI governance: four frameworks, one set of controls", 28, True)
    c.text(40, 74, "How the design answers ISO/IEC 42001, the NIST AI RMF, the NCA's AI "
           "Cybersecurity Guidelines and SDAIA's AI ethics and data rules.", 15,
           color=MUTED)  # fmt: skip
    x = 1070.0
    for state, label in ((IN_PLACE, "in place"), (PLANNED, "planned (module)"),
                         (PROPOSED, "proposed, not yet planned")):  # fmt: skip
        marker(c, x, 106, state)
        c.text(x + 12, 106, label, 12.5, color=MUTED)
        x += c.text_width(label, 12.5) + 36
    top = 128.0
    c.box(40, top, 1560, top + 46, fill=mix(PANEL, "#38BDF8", 0.12), outline=LINE)
    c.text(52, top + 23, "Theme", 14, True)
    for index, (name, note) in enumerate(FRAMEWORKS):
        left = COLUMNS[index + 1] + 10
        c.text(left, top + 15, name, 14, True)
        c.text(left, top + 33, note, 11.5, color=MUTED)
    c.text(COLUMNS[5] + 10, top + 23, "In WindOps AI", 14, True)
    y = top + 46
    for row, theme in enumerate(THEMES):
        cells = [
            c.wrap(text, CELL, COLUMNS[i + 2] - COLUMNS[i + 1] - 20)
            for i, text in enumerate((theme.iso, theme.nist, theme.nca, theme.sdaia))
        ]
        answers = [
            (c.wrap(a.text + (f"  ({a.module})" if a.module != GAP else ""), ANSWER,
                    COLUMNS[6] - COLUMNS[5] - 34), status(a, tracker))
            for a in theme.answers
        ]  # fmt: skip
        theme_rows = c.wrap(theme.name, 14, COLUMNS[1] - COLUMNS[0] - 24, bold=True)
        height = 20 + max(
            len(theme_rows) * 18,
            max(len(lines) for lines in cells) * LEADING,
            sum(len(lines) * LEADING + 6 for lines, _ in answers),
        )
        c.box(40, y, 1560, y + height, fill=PANEL if row % 2 == 0 else mix(PANEL, TEXT, 0.03),
              outline=LINE, radius=0)  # fmt: skip
        for index, value in enumerate(theme_rows):
            c.text(52, y + 18 + index * 18, value, 14, True)
        for index, lines in enumerate(cells):
            for line, value in enumerate(lines):
                c.text(COLUMNS[index + 1] + 10, y + 18 + line * LEADING, value, CELL,
                       color=MUTED)  # fmt: skip
        ay = y + 18
        for lines, state in answers:
            marker(c, COLUMNS[5] + 16, ay, state)
            for line, value in enumerate(lines):
                c.text(COLUMNS[5] + 30, ay + line * LEADING, value, ANSWER,
                       color=TEXT if state == IN_PLACE else MUTED)  # fmt: skip
            ay += len(lines) * LEADING + 6
        y += height
    for edge in COLUMNS[1:-1]:
        c.line([(edge, top), (edge, y)], LINE, 1)
    y += 30
    c.text(800, y, "An alignment for review, not a certification: organizations are certified to "
           "ISO/IEC 42001 by accredited bodies.", 14, True, AMBER, "mm")  # fmt: skip
    y += 24
    c.text(800, y, "The NIST AI RMF, the NCA guidelines and SDAIA's AI ethics principles are "
           "voluntary; the PDPL is law.", 14, True, AMBER, "mm")  # fmt: skip
    cropped = c.image.crop((0, 0, 1600 * c.s, int(y + 26) * c.s))
    ASSETS.mkdir(parents=True, exist_ok=True)
    cropped.save(ASSETS / "ai-governance.png", optimize=True)


if __name__ == "__main__":
    build_governance_view()
