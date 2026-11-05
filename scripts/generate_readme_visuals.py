#!/usr/bin/env python3
"""Generate the README's architecture diagrams and agent orchestration animation.

These visuals show the target design in docs/srs.md (sections 5, 6, 7 and 8).
Update this script when that design changes, then run it from any directory:

    uv run python scripts/generate_readme_visuals.py

Requires Pillow and ffmpeg. Writes to docs/assets/.
"""

import itertools
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image, ImageDraw, ImageFont
from provision_postgres import SERVICE_NAMESPACES, database_name

Point = tuple[float, float]

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"

BG = "#0B1220"
PANEL = "#131C2E"
LINE = "#33415C"
TEXT = "#F1F5F9"
MUTED = "#A9B7CC"

# kind: (accent, fill)
KINDS = {
    "ai": ("#A78BFA", "#2A2350"),
    "rule": ("#2DD4BF", "#0D3B3B"),
    "human": ("#FBBF24", "#46350A"),
    "service": ("#94A3B8", "#1E293B"),
    "external": ("#60A5FA", "#15294A"),
}

REGULAR_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
BOLD_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def mix(a: str, b: str, t: float) -> str:
    """Blend hex color a toward hex color b by fraction t."""
    ca = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb, strict=True))


class Canvas:
    """Pillow drawing surface addressed in logical, unscaled coordinates."""

    _fonts: ClassVar[dict[tuple[float, bool], ImageFont.FreeTypeFont]] = {}

    def __init__(self, width: int, height: int, scale: int = 2, background: str = BG) -> None:
        self.width, self.height, self.s = width, height, scale
        self.image = Image.new("RGB", (width * scale, height * scale), background)
        self.draw = ImageDraw.Draw(self.image)

    def font(self, size: float, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size * self.s, bold)
        if key not in self._fonts:
            for path in BOLD_FONTS if bold else REGULAR_FONTS:
                if Path(path).exists():
                    self._fonts[key] = ImageFont.truetype(path, size * self.s)
                    break
            else:
                raise FileNotFoundError("No supported font found")
        return self._fonts[key]

    def text_width(self, value: str, size: float, bold: bool = False) -> float:
        return self.draw.textlength(value, font=self.font(size, bold)) / self.s

    def text(
        self,
        x: float,
        y: float,
        value: str,
        size: float = 14,
        bold: bool = False,
        color: str = TEXT,
        anchor: str = "lm",
    ) -> None:
        self.draw.text(
            (x * self.s, y * self.s),
            value,
            font=self.font(size, bold),
            fill=color,
            anchor=anchor,
        )

    def wrap(self, value: str, size: float, width: float, bold: bool = False) -> list[str]:
        lines, current = [], ""
        for word in value.split():
            trial = f"{current} {word}".strip()
            if self.text_width(trial, size, bold) <= width:
                current = trial
            else:
                lines.append(current)
                current = word
        return [*lines, current]

    def box(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        fill: str | None = PANEL,
        outline: str = LINE,
        radius: float = 8,
        width: float = 1,
    ) -> None:
        s = self.s
        self.draw.rounded_rectangle(
            (x0 * s, y0 * s, x1 * s, y1 * s),
            radius=radius * s,
            fill=fill,
            outline=outline,
            width=max(1, round(width * s)),
        )

    def line(self, points: list[Point], color: str = LINE, width: float = 1.5) -> None:
        scaled = [(x * self.s, y * self.s) for x, y in points]
        self.draw.line(scaled, fill=color, width=max(1, round(width * self.s)), joint="curve")

    def head(self, tip: Point, origin: Point, color: str, size: float = 7) -> None:
        angle = math.atan2(tip[1] - origin[1], tip[0] - origin[0])
        corners = [tip]
        for offset in (2.6, -2.6):
            corners.append(
                (tip[0] + size * math.cos(angle + offset), tip[1] + size * math.sin(angle + offset))
            )
        self.draw.polygon([(x * self.s, y * self.s) for x, y in corners], fill=color)

    def arrow(
        self, points: list[Point], color: str = MUTED, width: float = 1.5, both: bool = False
    ) -> None:
        self.line(points, color, width)
        self.head(points[-1], points[-2], color)
        if both:
            self.head(points[0], points[1], color)

    def dot(self, x: float, y: float, radius: float, color: str) -> None:
        s = self.s
        self.draw.ellipse(
            ((x - radius) * s, (y - radius) * s, (x + radius) * s, (y + radius) * s),
            fill=color,
        )

    def node(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        title: str,
        sub: str | None = None,
        kind: str = "service",
        title_size: float = 15,
        sub_size: float = 12.5,
    ) -> None:
        accent, fill = KINDS[kind]
        self.box(x0, y0, x1, y1, fill=fill, outline=accent, width=1.25)
        self.label(x0, y0, x1, y1, title, sub, TEXT, MUTED, title_size, sub_size)

    def label(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        title: str,
        sub: str | None,
        title_color: str,
        sub_color: str,
        title_size: float,
        sub_size: float,
    ) -> None:
        """Center a bold title and an optional sub-line, and report any overflow."""
        room = x1 - x0 - 12
        titles = self.wrap(title, title_size, room, bold=True)
        subs = self.wrap(sub, sub_size, room) if sub else []
        rows = [(t, title_size, True, title_color) for t in titles]
        rows += [(t, sub_size, False, sub_color) for t in subs]
        height = sum(r[1] * 1.25 for r in rows)
        if height > y1 - y0:
            print(f"WARNING: text overflows its box: {title!r}")
        y = (y0 + y1) / 2 - height / 2
        for value, size, bold, color in rows:
            self.text((x0 + x1) / 2, y + size * 0.625, value, size, bold, color, "mm")
            y += size * 1.25

    def frame(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        title: str,
        color: str = MUTED,
        outline: str = LINE,
        fill: str = PANEL,
    ) -> None:
        self.box(x0, y0, x1, y1, fill=fill, outline=outline, radius=10)
        self.text(x0 + 14, y0 + 17, title, 14, True, color)


# ---------------------------------------------------------------- architecture


def build_architecture() -> None:
    c = Canvas(1400, 1058)

    c.text(40, 34, "WindOps AI architecture", 30, True)
    c.text(40, 66, "Target design from SRS v1.1, built in phases.", 16, color=MUTED)

    legend = [
        ("ai", "AI agent"),
        ("rule", "Deterministic decision"),
        ("human", "Human decision"),
        ("service", "Service"),
        ("external", "External"),
    ]
    x = 1360 - sum(c.text_width(n, 14) + 38 for _, n in legend)
    for kind, name in legend:
        accent, fill = KINDS[kind]
        c.box(x, 26, x + 18, 44, fill=fill, outline=accent, radius=4, width=1.25)
        c.text(x + 24, 35, name, 14, color=MUTED)
        x += c.text_width(name, 14) + 38

    # Edge
    top, bottom = 106, 164
    mid = (top + bottom) / 2
    c.node(40, top, 220, bottom, "Operators and approvers", kind="human")
    c.node(280, top, 470, bottom, "React frontend", "tenant-branded")
    c.node(590, top, 800, bottom, "API Gateway", "Apache APISIX, OIDC checks")
    c.node(870, top, 1040, bottom, "Web BFF", "no business rules")
    c.node(1140, top, 1360, bottom, "Telemetry simulator", "synthetic SCADA and CMS")
    c.arrow([(220, mid), (280, mid)])
    c.arrow([(470, mid), (590, mid)])
    c.text(530, mid + 13, "HTTPS", 12.5, color=MUTED, anchor="mm")
    c.arrow([(800, mid), (870, mid)])
    c.arrow([(1250, top), (1250, 90), (695, 90), (695, top)])
    c.text(975, 80, "authenticated device route to Telemetry", 12.5, color=MUTED, anchor="mm")

    # Agent Runtime
    c.frame(
        40,
        196,
        1060,
        372,
        "Agent Runtime: LangGraph, Deep Agents and Claude Opus 5.5",
        KINDS["ai"][0],
        KINDS["ai"][0],
    )
    c.node(450, 222, 650, 268, "Supervisor", "plans, delegates, synthesizes", "ai")
    specialists = [
        ("SCADA Analyst", "telemetry and alarms"),
        ("Reliability Engineer", "failure modes"),
        ("Safety and Procedure Researcher", "read-only"),
        ("Production Impact", "explains results"),
        ("Maintenance Planner", "draft only"),
    ]
    width, gap = 188, 10
    for i, (name, role) in enumerate(specialists):
        x0 = 54 + i * (width + gap)
        c.line(
            [(550, 268), (550, 282), (x0 + width / 2, 282), (x0 + width / 2, 296)], KINDS["ai"][0]
        )
        c.node(x0, 296, x0 + width, 362, name, role, "ai", title_size=14.5)

    # MCP
    c.frame(
        100,
        412,
        1060,
        494,
        "MCP servers: authenticated, schema-validated, per-agent allowlists",
        TEXT,
    )
    servers = [
        ("WindOps MCP", "tenant-scoped, draft only"),
        ("Documentation MCP", "document permissions"),
        ("Tavily MCP", "external web research"),
        ("Weather MCP", "weather only"),
    ]
    for i, (name, note) in enumerate(servers):
        x0 = 114 + i * 186
        c.node(x0, 440, x0 + 178, 487, name, note, title_size=14, sub_size=12)
    c.text(862, 447, "Development only:", 12.5, True, MUTED)
    c.text(862, 464, "Filesystem MCP", 12.5, color=MUTED)
    c.text(862, 480, "PostgreSQL MCP", 12.5, color=MUTED)
    c.arrow([(580, 372), (580, 412)])
    c.text(592, 392, "allowlisted tools", 12.5, color=MUTED)
    c.arrow([(580, 494), (580, 534)])
    c.text(592, 514, "gRPC, mTLS and circuit breakers", 12.5, color=MUTED)

    # Web BFF to services, and queued runs back to the runtime
    c.arrow([(1040, mid + 12), (1100, mid + 12), (1100, 534)])
    c.text(1092, 514, "gRPC", 12.5, color=MUTED, anchor="rm")
    c.arrow([(70, 534), (70, 372)])
    c.text(82, 392, "queued runs, resume events", 12.5, color=MUTED)

    # Guardrails
    c.frame(1140, 196, 1360, 494, "Agent guardrails", KINDS["human"][0])
    rules = [
        "Agents recommend and draft. They never approve.",
        "50 tool calls, 3 retries and 2 repairs by default.",
        "Token and cost budgets per tenant.",
        "An agent that reads external content has no write tools.",
        "Each run pins its graph, prompt, model and tool versions.",
    ]
    y: float = 234
    for rule in rules:
        for row, value in enumerate(c.wrap(rule, 13.5, 188)):
            if row == 0:
                c.dot(1156, y, 2.5, KINDS["human"][0])
            c.text(1166, y, value, 13.5)
            y += 18.5
        y += 8

    # Services
    c.frame(
        40,
        534,
        1360,
        758,
        "Services: these 20, plus API Gateway, Web BFF and Agent Runtime above, make 23",
        TEXT,
    )
    groups = [
        ("Identity and tenancy", [("Identity", ""), ("Authorization", ""), ("Tenant", "")]),
        (
            "Operations",
            [("Asset", ""), ("Telemetry", ""), ("Alarm", ""), ("Fault Detection", "rule")],
        ),
        (
            "Investigation",
            [("Investigation", ""), ("Agent Sandbox Adapter", ""), ("Evaluation", "")],
        ),
        (
            "Knowledge and research",
            [("Knowledge", ""), ("Weather", ""), ("External Intelligence", "")],
        ),
        ("Deterministic checks", [("Production Impact", "rule"), ("Policy", "rule")]),
        ("Maintenance", [("Maintenance", ""), ("Inventory", ""), ("Approval", "human")]),
        ("Record and notify", [("Audit", ""), ("Notification", "")]),
    ]
    width, gap = 180, 7
    for i, (name, members) in enumerate(groups):
        x0 = 52 + i * (width + gap)
        c.box(x0, 562, x0 + width, 748, fill=BG, radius=8)
        c.text(x0 + width / 2, 578, name, 13.5, True, MUTED, "mm")
        for row, (service, kind) in enumerate(members):
            y0 = 592 + row * 38.5
            c.node(
                x0 + 4,
                y0,
                x0 + width - 4,
                y0 + 33,
                service,
                None,
                kind or "service",
                title_size=13.5,
            )

    # Events
    c.arrow([(700, 758), (700, 780)], both=True)
    c.box(40, 780, 1360, 824, fill=PANEL, outline=MUTED)
    c.label(
        40,
        780,
        1360,
        824,
        "NATS JetStream: durable events and work queues, with Protobuf and a transactional outbox",
        None,
        TEXT,
        MUTED,
        15,
        12,
    )

    # Data and external providers
    c.frame(40, 840, 660, 950, "Data: each service owns its database")
    stores = [
        ("PostgreSQL", "system of record, telemetry"),
        ("Redis", "cache, rate limits"),
        ("Qdrant", "vector retrieval"),
    ]
    for i, (name, note) in enumerate(stores):
        x0 = 52 + i * 200
        c.node(x0, 868, x0 + 194, 940, name, note, title_size=14)

    c.frame(
        676, 840, 1360, 950, "External and identity providers: reached through circuit breakers"
    )
    providers = [
        ("Claude API", "Agent Runtime"),
        ("Tavily", "External Intelligence"),
        ("Open-Meteo", "Weather"),
        ("OpenAI", "embeddings, Knowledge"),
        ("LangSmith", "traces, evals, Sandbox"),
        ("Keycloak", "OIDC and MFA"),
    ]
    for i, (name, note) in enumerate(providers):
        x0 = 688 + i * 111
        c.node(x0, 868, x0 + 105, 940, name, note, "external", title_size=14, sub_size=12)

    # Footer
    c.box(40, 964, 1360, 1000, fill=PANEL)
    c.label(
        40,
        964,
        1360,
        1000,
        "Observability: OpenTelemetry, Tempo, Prometheus, Grafana, Loki, Fluent Bit, Alertmanager. "
        "Runtime: Docker and Kubernetes on Minikube.",
        None,
        MUTED,
        MUTED,
        14,
        12,
    )
    c.text(
        700,
        1028,
        "Agents recommend. Deterministic services and authorized humans decide. "
        "The system never issues turbine commands.",
        16,
        True,
        KINDS["human"][0],
        "mm",
    )
    ASSETS.mkdir(parents=True, exist_ok=True)
    c.image.save(ASSETS / "architecture.png", optimize=True)


# ------------------------------------------------------------- security by layer

GREEN = "#34D399"
AMBER = "#FBBF24"
TRACKER = ASSETS.parent / "phases" / "README.md"


@dataclass(frozen=True)
class Control:
    text: str
    module: str | None = None  # The tracker modules that deliver it, or None when in place.


@dataclass(frozen=True)
class Layer:
    title: str
    nodes: tuple[tuple[str, str | None, str], ...]
    controls: tuple[Control, ...]
    inside_cluster: bool = True


SECURITY_LAYERS = (
    Layer(
        "1. Client",
        (
            ("React frontend", "TypeScript, role-aware screens", "service"),
            ("Operators and approvers", "8 roles, per tenant", "human"),
        ),
        (
            Control("Login through Keycloak (OIDC), with MFA for administrators"),
            Control("The browser reaches only the gateway, never internal services", "M3.7"),
        ),
        inside_cluster=False,
    ),
    Layer(
        "2. Edge",
        (
            ("API Gateway", "Apache APISIX, etcd", "service"),
            ("Web BFF", "FastAPI: REST, OpenAPI, SSE", "service"),
        ),
        (
            Control("The gateway is the only way in, and nothing else is exposed"),
            Control("Its Admin API needs a key, and answers only from inside its pod"),
            Control("Every route needs health checks and timeouts, so failures are fast"),
            Control("TLS, CORS allowlists, rate limits and token validation", "M3.1, M3.7"),
        ),
    ),
    Layer(
        "3. AI agents",
        (
            ("Supervisor and 5 specialists", "LangGraph, Deep Agents, Claude", "ai"),
            ("MCP servers", "allowlisted tools, schema-checked", "service"),
        ),
        (
            Control(
                "Agents can only draft, and can never approve or command a turbine", "M8.6, M10.1"
            ),
            Control("Each agent gets only its own tools, inside its tenant", "M8.5, M8.6"),
            Control("An agent that reads web content has no write tools", "M8.6"),
            Control("Retrieved content is untrusted, and tenant budgets limit runs", "M7.5, M8.3"),
        ),
    ),
    Layer(
        "4. Microservices",
        (
            ("Policy, Approval, Fault Detection", "deterministic rules and humans", "rule"),
            ("Domain services, 1 image each", "Python, gRPC, versioned Protobuf", "service"),
        ),
        (
            Control("Every call needs a signed identity and a deadline, or it is rejected"),
            Control("Tenant comes from the verified identity, never from a request field"),
            Control("Circuit breakers stop an outage from spreading between services"),
            Control("Internal error details stay in the logs, and never reach the caller"),
            Control("Encrypted calls between services, using mTLS"),
            Control("Permissions denied by default, decided by the Authorization Service"),
            Control("Nobody can approve their own request", "M10.2"),
        ),
    ),
    Layer(
        "5. Data and events",
        (
            ("NATS JetStream", "durable events, transactional outbox", "service"),
            ("Data stores", "PostgreSQL, Redis, Qdrant", "service"),
        ),
        (
            Control("Each service has its own database and credentials"),
            Control("Tenant isolation in the database, with row-level security", "M3.6"),
            Control("Audit records are append-only and tamper-evident"),
        ),
    ),
    Layer(
        "6. External providers",
        (
            ("Claude, OpenAI, Tavily", "models, embeddings, web research", "external"),
            ("Open-Meteo, LangSmith", "forecasts, agent traces", "external"),
        ),
        (
            Control("Keys stay in ignored files and Kubernetes Secrets, never in images"),
            Control("Outbound traffic only to named endpoints", "M13.3"),
            Control("Tenant details are removed before data leaves the cluster", "M7.4, M8.9"),
        ),
        inside_cluster=False,
    ),
)

CLUSTER_CONTROLS = (
    Control("Services run as non-root, with a read-only filesystem and no capabilities"),
    Control("Pod Security is enforced where our own workloads run"),
    Control("Memory limits and health probes on every workload"),
    Control("Calico can enforce network rules, proven with a test"),
    Control("Deny all traffic between namespaces by default", "M13.3"),
    Control("Signed container images", "M14.2"),
)
DELIVERY_CONTROLS = (
    Control("No direct pushes to main, and every change needs a pull request"),
    Control("Lint, types, tests, CodeQL, dependency audit, secret and image scans must pass"),
    Control("Pinned dependencies, base images and CI actions"),
)
OBSERVABILITY_CONTROLS = (
    Control("Known secret and personal-data patterns are removed from logs and traces"),
    Control("Logs, traces and metrics share one trace ID, and Grafana needs a login"),
    Control("Every approval and agent action in the audit record", "M2.7, M8.5"),
)


def all_controls() -> list[Control]:
    hops = [control for _, controls in HOP_CONTROLS for control in controls]
    return (
        [control for layer in SECURITY_LAYERS for control in layer.controls]
        + list(CLUSTER_CONTROLS + DELIVERY_CONTROLS + OBSERVABILITY_CONTROLS)
        + hops
    )


def read_phase_progress(tracker: Path = TRACKER) -> list[tuple[int, str, int, int]]:
    """Return each phase's number, name, finished modules and total modules from the tracker."""
    text = tracker.read_text()
    phases = []
    for match in re.finditer(r"^## Phase (\d+): (.+)$", text, re.M):
        following = text[match.end() :]
        next_heading = following.find("\n## ")
        section = following if next_heading == -1 else following[:next_heading]
        done = len(re.findall(r"^- \[x\] ", section, re.M))
        total = done + len(re.findall(r"^- \[ \] ", section, re.M))
        phases.append((int(match.group(1)), match.group(2), done, total))
    return phases


def draw_control_marker(c: Canvas, x: float, y: float, planned: bool) -> None:
    if planned:
        c.dot(x, y, 5.5, AMBER)
        c.dot(x, y, 3.5, PANEL)
    else:
        c.dot(x, y, 5.5, GREEN)


def controls_height(c: Canvas, controls: tuple[Control, ...], width: float) -> float:
    rows = 0
    for control in controls:
        suffix = f"  ({control.module})" if control.module else ""
        rows += len(c.wrap(control.text + suffix, 14.5, width - 22))
    return rows * 20 + (len(controls) - 1) * 4


def draw_controls(
    c: Canvas, x: float, y: float, controls: tuple[Control, ...], width: float
) -> None:
    for control in controls:
        suffix = f"  ({control.module})" if control.module else ""
        planned = control.module is not None
        rows = c.wrap(control.text + suffix, 14.5, width - 22)
        draw_control_marker(c, x + 6, y + 9, planned)
        for index, row in enumerate(rows):
            c.text(x + 22, y + 9 + index * 20, row, 14.5, color=MUTED if planned else TEXT)
        y += len(rows) * 20 + 4


def draw_roadmap(c: Canvas, y: float) -> float:
    """Draw every delivery phase with its progress, and return the y below it."""
    phases = read_phase_progress()
    finished = sum(done for _, _, done, _ in phases)
    total = sum(count for _, _, _, count in phases)
    c.text(40, y + 8, f"DELIVERY PLAN: {finished} of {total} modules done", 12.5, True, MUTED)
    y += 22
    per_row, gap, height = 7, 8.0, 62.0
    width = (1320 - gap * (per_row - 1)) / per_row
    for index, (number, name, done, count) in enumerate(phases):
        x0 = 40 + (index % per_row) * (width + gap)
        y0 = y + (index // per_row) * (height + gap)
        if done == count:
            accent, fill, state = GREEN, "#0D3B2E", "done"
        elif done:
            accent, fill, state = AMBER, KINDS["human"][1], "in progress"
        else:
            accent, fill, state = LINE, PANEL, "planned"
        c.box(x0, y0, x0 + width, y0 + height, fill=fill, outline=accent)
        c.label(
            x0, y0, x0 + width, y0 + height,
            f"{number}. {name}", f"{done} of {count} modules, {state}", TEXT, MUTED, 12.5, 11.5,
        )  # fmt: skip
    rows = math.ceil(len(phases) / per_row)
    return y + rows * height + (rows - 1) * gap


def build_security_layers() -> None:
    controls = all_controls()
    in_place = sum(control.module is None for control in controls)
    planned = len(controls) - in_place

    measure = Canvas(10, 10, scale=1)
    right_width = 612.0
    heights = [
        max(102.0, controls_height(measure, layer.controls, right_width) + 42)
        for layer in SECURITY_LAYERS
    ]
    gap = 34
    top = 134
    canvas_height = int(top + sum(heights) + gap * (len(heights) - 1) + 620)
    c = Canvas(1400, canvas_height)

    c.text(40, 34, "WindOps AI: system and security architecture", 28, True)
    c.text(
        40, 68,
        f"Target design from SRS v1.1: 23 services, {in_place} security controls in place, "
        f"{planned} planned.",
        16, color=MUTED,
    )  # fmt: skip
    c.text(
        40, 92,
        "In place means built and tested in the shared code and templates. "
        "Only the Audit Service and sample services run so far.",
        14.5, color=MUTED,
    )  # fmt: skip
    draw_control_marker(c, 1048, 36, False)
    c.text(1062, 36, "In place today", 14.5)
    draw_control_marker(c, 1198, 36, True)
    c.text(1212, 36, "Planned (module)", 14.5, color=MUTED)
    c.text(50, 120, "HOW THE PIECES WORK TOGETHER", 12.5, True, MUTED)
    c.text(722, 120, "SECURITY AT THIS LAYER", 12.5, True, MUTED)

    positions = []
    y = float(top)
    for height in heights:
        positions.append((y, y + height))
        y += height + gap

    inside = [
        place
        for place, layer in zip(positions, SECURITY_LAYERS, strict=True)
        if layer.inside_cluster
    ]
    cluster_top, cluster_bottom = inside[0][0] - 25, inside[-1][1] + 14
    c.box(
        40,
        cluster_top,
        690,
        cluster_bottom,
        fill=None,
        outline=KINDS["external"][0],
        radius=12,
        width=1.5,
    )
    c.box(
        356, cluster_top - 11, 678, cluster_top + 11,
        fill=KINDS["external"][1], outline=KINDS["external"][0], radius=6,
    )  # fmt: skip
    c.text(517, cluster_top, "Kubernetes (Minikube locally): 5 namespaces", 13, True, TEXT, "mm")

    node_y = []
    for (start, end), layer in zip(positions, SECURITY_LAYERS, strict=True):
        c.text(62, start + 13, layer.title, 14.5, True, MUTED)
        node_y.append((start + 28, start + 92))
        for index, (name, note, kind) in enumerate(layer.nodes):
            x0 = 62 + index * 308
            c.node(
                x0,
                start + 28,
                x0 + 296,
                start + 92,
                name,
                note,
                kind,
                title_size=14.5,
                sub_size=12.5,
            )
        c.box(708, start, 1360, end, fill=PANEL)
        c.text(722, start + 14, layer.title, 13.5, True, MUTED)
        draw_controls(c, 722, start + 28, layer.controls, right_width)

    left_x, right_x = 250.0, 518.0
    middle = [(first + last) / 2 for first, last in node_y]
    # Within a layer.
    c.arrow([(370, middle[0]), (358, middle[0])])  # People use the frontend.
    c.arrow([(358, middle[1]), (370, middle[1])])  # The gateway forwards to Web BFF.
    c.arrow([(358, middle[2]), (370, middle[2])])  # Agents call MCP tools.
    c.arrow([(370, middle[3]), (358, middle[3])])  # Services ask for decisions.
    # Between layers.
    c.arrow([(left_x, node_y[0][1]), (left_x, node_y[1][0])])
    c.text(
        left_x + 10, node_y[1][0] - 14,
        "Trust boundary: HTTPS, REST, server-sent events", 13, color=AMBER,
    )  # fmt: skip
    c.arrow([(666, middle[1]), (679, middle[1]), (679, middle[3]), (666, middle[3])])
    c.text(
        672, node_y[2][0] - 14,
        "gRPC with signed identity, deadline and trace", 13, color=MUTED, anchor="rm",
    )  # fmt: skip
    c.arrow([(right_x, node_y[2][1]), (right_x, node_y[3][0])])
    c.text(
        right_x - 10, (node_y[2][1] + node_y[3][0]) / 2,
        "Trust boundary: agents reach services only through MCP", 13, color=AMBER, anchor="rm",
    )  # fmt: skip
    c.arrow([(right_x, node_y[3][1]), (right_x, node_y[4][0])], both=True)
    c.text(
        right_x - 10, (node_y[3][1] + node_y[4][0]) / 2,
        "Each service reaches only its own data, and shares events", 13, color=MUTED, anchor="rm",
    )  # fmt: skip
    c.arrow([(left_x, cluster_bottom), (left_x, node_y[5][0])])
    c.text(
        left_x + 10, node_y[5][0] - 14,
        "Trust boundary: named, redacted, audited egress only", 13, color=AMBER,
    )  # fmt: skip

    y = positions[-1][1] + 22
    columns = (
        ("Kubernetes, around layers 2 to 5", CLUSTER_CONTROLS, 40.0, 450.0),
        ("Delivery, before code reaches main", DELIVERY_CONTROLS, 502.0, 420.0),
        ("Observability, across every layer", OBSERVABILITY_CONTROLS, 934.0, 426.0),
    )
    panel_height = 42 + max(controls_height(c, group, width - 24) for _, group, _, width in columns)
    for title, group, column_x, width in columns:
        c.box(column_x, y, column_x + width, y + panel_height, fill=PANEL)
        c.text(column_x + 14, y + 16, title, 13.5, True, MUTED)
        draw_controls(c, column_x + 12, y + 30, group, width - 24)
    y = draw_roadmap(c, y + panel_height + 14) + 28
    c.text(
        700, y,
        "Agents recommend. Deterministic services and authorized humans decide. "
        "The system never issues turbine commands.",
        16, True, AMBER, "mm",
    )  # fmt: skip

    cropped = c.image.crop((0, 0, 1400 * c.s, int(y + 28) * c.s))
    ASSETS.mkdir(parents=True, exist_ok=True)
    cropped.save(ASSETS / "security-layers.png", optimize=True)


# ---------------------------------------------------------------- end to end

RED = "#F87171"
EVENT = "#C4B5FD"

# Security on each kind of hop. The tests hold these to the tracker, like the other controls.
HOP_CONTROLS = (
    (
        "Coming in",
        (
            Control("The gateway is the only way in, and nothing else is exposed"),
            Control("Every route needs health checks and timeouts"),
            Control("Login through Keycloak (OIDC), with MFA for administrators"),
            Control("TLS, CORS allowlists, rate limits and token validation", "M3.1, M3.7"),
        ),
    ),
    (
        "Between services",
        (
            Control("Every call needs a signed identity and a deadline, or it is rejected"),
            Control("Tenant comes from the verified identity, never from a request field"),
            Control("Encrypted calls between services, using mTLS"),
            Control("Permissions denied by default, decided by the Authorization Service"),
        ),
    ),
    (
        "Agents",
        (
            Control("Agents reach services only through allowlisted MCP tools", "M7.5, M8.6"),
            Control("An agent that reads web content has no write tools", "M8.6"),
            Control("Agents can only draft, and can never approve", "M9.5, M10.1"),
            Control("Web content is untrusted, and is logged and redacted", "M7.4"),
        ),
    ),
    (
        "Data and events",
        (
            Control("Each service has its own database and credentials"),
            Control("Databases are not reachable from outside the cluster"),
            Control("Tenant isolation in the database, with row-level security", "M3.6"),
            Control("Audit records are append-only and tamper-evident"),
        ),
    ),
)
# Every service in SRS 7.2 is drawn from these, so a test can prove none is missing.
ENTRY_SERVICES = ("API Gateway", "Web BFF", "Identity", "Authorization", "Tenant")
AGENT_SERVICE = "Agent Runtime"
APPLICATION_GROUPS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Operations",
        "service",
        (
            ("Asset", "service"),
            ("Telemetry", "service"),
            ("Alarm", "service"),
            ("Fault Detection", "rule"),
            ("Investigation", "service"),
        ),
    ),
    (
        "Knowledge and research",
        "service",
        (("Knowledge", "service"), ("Weather", "service"), ("External Intelligence", "service")),
    ),
    ("Deterministic checks", "rule", (("Production Impact", "rule"), ("Policy", "rule"))),
    (
        "Maintenance",
        "service",
        (("Maintenance", "service"), ("Inventory", "service"), ("Approval", "human")),
    ),
    ("Record and notify", "service", (("Audit", "service"), ("Notification", "service"))),
    (
        "Agent support",
        "service",
        (("Agent Sandbox Adapter", "service"), ("Evaluation", "service")),
    ),
)
BREAKER_OUTCOMES = (
    (
        "Refuse, because it protects safety or access",
        RED,
        (
            ("Authorization", "deny the operation"),
            ("Policy", "nothing reaches human review"),
            ("Production Impact", "block; agents cannot guess"),
            ("Keycloak", "no new logins, no weaker access"),
            ("PostgreSQL", "return an error, write nothing"),
        ),
    ),
    (
        "Pause or refuse new work, and keep what exists",
        AMBER,
        (
            ("Claude API", "pause the run, and resume later"),
            ("Knowledge, Qdrant", "no uncited claims"),
            ("Telemetry, Alarm", "no new snapshots; old ones readable"),
            ("Approval", "reject decisions; queue readable"),
            ("Forecast provider", "weather checks block or escalate"),
        ),
    ),
    (
        "Continue, and record the gap or queue the work",
        GREEN,
        (
            ("Tavily", "record a research gap"),
            ("Sandbox", "continue without the analysis"),
            ("Audit", "hold in the outbox, alert on backlog"),
            ("Notification", "queue in JetStream"),
            ("JetStream", "hold events in the outbox"),
        ),
    ),
)


def dashed_arrow(c: Canvas, start: Point, end: Point, color: str = EVENT) -> None:
    """Draw a dashed arrow for an event, which the sender does not wait on."""
    length = math.dist(start, end)
    dashes = max(1, int(length // 9))
    for index in range(0, dashes, 2):
        a = index / dashes
        b = min(1.0, (index + 1) / dashes)
        c.line(
            [
                (start[0] + (end[0] - start[0]) * a, start[1] + (end[1] - start[1]) * a),
                (start[0] + (end[0] - start[0]) * b, start[1] + (end[1] - start[1]) * b),
            ],
            color,
            1.5,
        )
    c.head(end, start, color)


def edge_label(c: Canvas, x: float, y: float, value: str, color: str = MUTED) -> None:
    width = c.text_width(value, 12)
    c.box(x - width / 2 - 5, y - 9, x + width / 2 + 5, y + 9, fill=BG, outline=BG, radius=4)
    c.text(x, y, value, 12, color=color, anchor="mm")


def draw_communication(c: Canvas, top: float) -> float:
    """Draw every service by namespace, with how they call each other and where data lives."""
    c.frame(40, top, 1360, top + 760, "1. How services communicate, and where the data lives")

    # Coming in
    y = top + 34
    c.node(60, y + 24, 190, y + 72, "Operators, approvers", kind="human", title_size=13)
    c.node(60, y + 88, 190, y + 136, "Telemetry simulator", title_size=13)
    c.node(235, y + 24, 375, y + 72, "React frontend", title_size=13)
    c.box(450, y, 1340, y + 150, fill=BG, radius=8)
    c.text(462, y + 14, "windops-system", 12.5, True, MUTED)
    c.node(
        470,
        y + 36,
        640,
        y + 96,
        "API Gateway",
        "APISIX, the only way in",
        title_size=14,
        sub_size=11.5,
    )
    c.node(740, y + 36, 900, y + 96, "Web BFF", "no business rules", title_size=14, sub_size=11.5)
    x = 985
    for name in ENTRY_SERVICES[2:]:
        c.node(x, y + 36, x + 110, y + 76, name, title_size=12.5)
        x += 118
    c.node(985, y + 96, 1095, y + 136, "Keycloak", title_size=12.5)
    c.text(1108, y + 116, "Every service asks Authorization first", 11.5, color=MUTED)
    c.arrow([(190, y + 48), (235, y + 48)])
    c.arrow([(375, y + 48), (470, y + 48)])
    edge_label(c, 415, y + 30, "HTTPS")
    c.arrow([(190, y + 112), (555, y + 112), (555, y + 96)])
    edge_label(c, 340, y + 112, "authenticated device route")
    c.arrow([(640, y + 66), (740, y + 66)])
    edge_label(c, 690, y + 48, "REST, SSE")
    c.arrow([(900, y + 56), (985, y + 56)])
    edge_label(c, 942, y + 38, "gRPC")
    c.arrow([(1040, y + 76), (1040, y + 96)])

    # Agents
    y = top + 200
    c.node(
        60,
        y + 12,
        320,
        y + 84,
        AGENT_SERVICE,
        "Supervisor and 5 agents, in windops-application",
        kind="ai",
    )
    c.box(390, y, 1000, y + 96, fill=BG, radius=8)
    c.text(402, y + 14, "windops-mcp: an agent's only route to services", 12.5, True, MUTED)
    x = 404
    for name, note in (
        ("WindOps MCP", "tenant-scoped, draft only"),
        ("Documentation MCP", "document permissions"),
        ("Tavily MCP", "web research"),
        ("Weather MCP", "weather only"),
    ):
        c.node(x, y + 30, x + 140, y + 86, name, note, title_size=12.5, sub_size=10.5)
        x += 147
    c.arrow([(320, y + 48), (390, y + 48)])
    edge_label(c, 355, y + 30, "tools")

    # Application services
    y = top + 322
    c.box(60, y, 1340, y + 222, fill=BG, radius=8)
    c.text(
        72,
        y + 15,
        "windops-application: each service has its own pod, and a breaker for each dependency",
        12.5,
        True,
        MUTED,
    )
    c.arrow([(695, y - 26), (695, y)])
    edge_label(c, 740, y - 13, "gRPC")
    # Web BFF's calls pass to the right of the MCP servers.
    c.arrow([(860, top + 130), (860, top + 190), (1100, top + 190), (1100, y)])
    edge_label(c, 1145, top + 260, "gRPC, with a breaker")
    x = 72
    for group, kind, members in APPLICATION_GROUPS:
        c.box(
            x,
            y + 30,
            x + 202,
            y + 212,
            fill=PANEL,
            outline=KINDS[kind][0] if kind != "service" else LINE,
        )
        c.text(x + 101, y + 44, group, 12.5, True, MUTED, "mm")
        row = y + 58
        for name, member_kind in members:
            c.node(x + 8, row, x + 194, row + 26, name, kind=member_kind, title_size=12.5)
            row += 30
        x += 210

    # Events
    y = top + 562
    c.box(300, y, 1180, y + 36, fill=mix(BG, EVENT, 0.14), outline=EVENT, radius=8)
    c.text(
        740,
        y + 18,
        "NATS JetStream: events and queued runs, from each service's outbox. The sender "
        "never waits.",
        13,
        True,
        anchor="mm",
    )
    dashed_arrow(c, (740, y - 18), (740, y))
    dashed_arrow(c, (640, y), (640, y - 18))

    # Data and external providers
    y = top + 616
    c.box(60, y, 1010, y + 132, fill=BG, radius=8)
    c.text(72, y + 14, "windops-data", 12.5, True, MUTED)
    c.arrow([(180, top + 544), (180, y)])
    edge_label(c, 240, top + 580, "own login only")
    c.box(72, y + 28, 760, y + 122, fill=KINDS["service"][1], outline=GREEN, width=1.5)
    c.text(84, y + 42, "PostgreSQL pod: one server, 20 separate databases", 13.5, True)
    c.text(
        84,
        y + 62,
        "windops_telemetry, windops_alarm, windops_audit, windops_approval and 16 more",
        12,
        color=MUTED,
    )
    c.text(
        84,
        y + 80,
        "A login opens only its own database. Service data is never read directly by another",
        12,
        color=MUTED,
    )
    c.text(
        84,
        y + 98,
        "service: it asks that service over gRPC, or listens for its events.",
        12,
        color=MUTED,
    )
    c.node(
        772,
        y + 28,
        886,
        y + 122,
        "Redis",
        "cache and rate limits only",
        title_size=13,
        sub_size=11.5,
    )
    c.node(894, y + 28, 1000, y + 122, "Qdrant", "vector search", title_size=13, sub_size=11.5)

    c.box(1030, y, 1340, y + 132, fill=BG, radius=8)
    c.text(1042, y + 14, "External, through breakers", 12.5, True, MUTED)
    c.arrow([(1260, top + 544), (1260, y)])
    names = ("Claude", "Tavily", "Forecast", "OpenAI", "LangSmith", "Email")
    for index, name in enumerate(names):
        col, row = index % 3, index // 3
        x0 = 1040 + col * 98
        c.node(
            x0, y + 30 + row * 48, x0 + 92, y + 72 + row * 48, name, kind="external", title_size=12
        )
    return top + 760


def draw_call_steps(c: Canvas, top: float) -> float:
    """Draw one call from start to finish, then the security on each kind of hop."""
    c.frame(
        40, top, 1360, top + 380, "2. One gRPC call, step by step, and the security on each hop"
    )
    steps = (
        ("1. Caller's breaker", "If it is open, fail at once and run the fallback", "service"),
        ("2. The call carries", "Deadline, trace, request ID and signed identity", "service"),
        ("3. Receiver checks", "No valid identity or deadline means rejected", "rule"),
        ("4. Authorization", "May this user do this, for this tenant?", "rule"),
        ("5. Own database", "The work and its event commit together", "service"),
        (
            "6. Outbox to JetStream",
            "Others hear about it, and the caller gets its answer",
            "service",
        ),
    )
    width = (1280 - 5 * 22) / 6
    x = 60.0
    for index, (title, note, kind) in enumerate(steps):
        c.node(
            x, top + 34, x + width, top + 124, title, note, kind=kind, title_size=13, sub_size=12
        )
        if index < len(steps) - 1:
            c.arrow([(x + width, top + 79), (x + width + 22, top + 79)])
        x += width + 22

    c.dot(66, top + 146, 5.5, GREEN)
    c.text(78, top + 146, "built and tested in the shared code", 12.5, color=MUTED)
    draw_control_marker(c, 330, top + 146, True)
    c.text(342, top + 146, "planned, with the module that delivers it", 12.5, color=MUTED)
    column = 1280 / 4
    for index, (title, controls) in enumerate(HOP_CONTROLS):
        x = 60 + index * column
        c.text(x, top + 172, title, 14, True, KINDS["external"][0])
        draw_controls(c, x, top + 186, controls, column - 18)
    return top + 380


def draw_breaker(c: Canvas, top: float) -> float:
    """Draw the breaker's states, then what the caller does when each dependency is down."""
    c.frame(40, top, 1360, top + 330, "3. What a circuit breaker does when a dependency fails")
    closed = (70, top + 44, 250, top + 104)
    opened = (70, top + 170, 250, top + 230)
    half = (340, top + 108, 520, top + 168)
    for box, name, note, color in (
        (closed, "Closed", "calls go through, and are counted", GREEN),
        (opened, "Open", "calls fail at once, and the fallback runs", RED),
        (half, "Half-open", "3 test calls go through", AMBER),
    ):
        c.box(*box, fill=mix(BG, color, 0.16), outline=color, width=1.5)
        c.label(*box, name, note, TEXT, MUTED, 14, 11.5)
    c.arrow([(130, top + 104), (130, top + 170)], RED)
    c.arrow([(250, top + 200), (430, top + 200), (430, top + 168)], AMBER)
    c.arrow([(430, top + 108), (430, top + 74), (250, top + 74)], GREEN)
    c.arrow([(370, top + 168), (370, top + 186), (250, top + 186)], RED)
    c.text(140, top + 137, "at least 10 recent calls,", 11.5, color=RED)
    c.text(140, top + 151, "50% failed or slow", 11.5, color=RED)
    c.text(322, top + 214, "after 30 seconds", 11.5, color=AMBER, anchor="mm")
    c.text(345, top + 60, "all 3 succeed", 11.5, color=GREEN, anchor="mm")
    c.text(308, top + 176, "any fails", 11.5, color=RED, anchor="mm")
    rows = (
        "Each caller has its own breaker for each dependency,",
        "so one failure does not spread. Bad requests, such as",
        "permission denied, do not count as failures.",
        "Retries: 3 attempts at most, for idempotent calls only.",
    )
    for index, row in enumerate(rows):
        c.text(70, top + 254 + index * 18, row, 12.5, color=MUTED)

    width = 258
    for index, (title, color, items) in enumerate(BREAKER_OUTCOMES):
        x = 545 + index * (width + 9)
        c.box(x, top + 36, x + width, top + 318, fill=BG, outline=color, width=1.25)
        for row_index, row in enumerate(c.wrap(title, 13, width - 24, bold=True)):
            c.text(x + 12, top + 52 + row_index * 17, row, 13, True, color)
        y = top + 92
        for name, outcome in items:
            c.text(x + 12, y, name, 13, True)
            c.text(x + 12, y + 18, outcome, 12, color=MUTED)
            y += 45
    return top + 330


def draw_database_placement(c: Canvas, top: float) -> float:
    """Contrast the shared local server with the production design."""
    c.frame(
        40, top, 1360, top + 276, "4. Where the databases run, and what happens if the server fails"
    )
    c.box(60, top + 36, 680, top + 262, fill=BG, outline=GREEN, width=1.25)
    c.text(74, top + 54, "Local cluster: built", 14, True, GREEN)
    for index, name in enumerate(("Telemetry pod", "Alarm pod", "Audit pod")):
        row = top + 72 + index * 44
        c.node(74, row, 214, row + 36, name, title_size=12.5)
        c.arrow([(214, row + 18), (300, row + 18)])
    c.box(300, top + 72, 666, top + 196, fill=KINDS["service"][1], outline=LINE)
    c.text(483, top + 90, "One PostgreSQL pod, with a persistent volume", 13, True, anchor="mm")
    for index, name in enumerate(("windops_telemetry", "windops_alarm", "windops_audit")):
        c.text(483, top + 116 + index * 24, name, 12.5, color=MUTED, anchor="mm")
    for index, sentence in enumerate(
        (
            "If the pod fails, all services lose their database "
            "until Kubernetes restarts it. Data stays",
            "on the volume. Services write nothing meanwhile, and waiting "
            "events stay in JetStream.",
        )
    ):
        c.text(74, top + 222 + index * 18, sentence, 12.5, color=MUTED)

    c.box(700, top + 36, 1340, top + 262, fill=BG, outline=AMBER, width=1.25)
    c.text(714, top + 54, "Production design: planned, not built (ADR 0003)", 14, True, AMBER)
    servers = (
        ("Telemetry server", "its own primary and standby"),
        ("Audit server", "its own primary and standby"),
        ("Shared server", "other services, primary and standby"),
    )
    for index, (name, note) in enumerate(servers):
        x = 714 + index * 206
        c.node(x, top + 72, x + 198, top + 136, name, note, title_size=13, sub_size=11.5)
    c.node(
        714,
        top + 150,
        1326,
        top + 186,
        "Scheduled backups for every server, tested by restoring them (M14.3)",
        title_size=12.5,
    )
    for index, sentence in enumerate(
        (
            "A standby on another machine takes over automatically, "
            "so one failure stops fewer services.",
            "Separate databases make a move mostly a change of host and credentials.",
        )
    ):
        c.text(714, top + 222 + index * 18, sentence, 12.5, color=MUTED)
    return top + 276


def build_end_to_end() -> None:
    c = Canvas(1400, 1920)
    c.text(40, 34, "How WindOps AI works end to end", 30, True)
    c.text(
        40,
        66,
        "Target design from SRS v1.1. Only the platform, Audit and sample services run today.",
        16,
        color=MUTED,
    )
    x = 650
    for index, (kind, name) in enumerate(
        (
            ("ai", "AI agent"),
            ("rule", "Deterministic"),
            ("human", "Human decision"),
            ("external", "External"),
        )
    ):
        accent, fill = KINDS[kind]
        chip_x, chip_y = x + (index % 2) * 140, 34 + (index // 2) * 26
        c.box(chip_x, chip_y - 8, chip_x + 16, chip_y + 8, fill=fill, outline=accent, radius=4)
        c.text(chip_x + 23, chip_y, name, 13.5, color=MUTED)
    c.arrow([(950, 34), (1000, 34)])
    c.text(1008, 34, "Call: the caller waits (gRPC)", 13.5, color=MUTED)
    dashed_arrow(c, (950, 60), (1000, 60))
    c.text(1008, 60, "Event: the sender does not wait", 13.5, color=MUTED)
    y = draw_communication(c, 90)
    y = draw_call_steps(c, y + 14)
    y = draw_breaker(c, y + 14)
    y = draw_database_placement(c, y + 14)
    c.text(
        700,
        y + 24,
        "Agents recommend. Deterministic services and authorized humans decide.",
        16,
        True,
        KINDS["human"][0],
        "mm",
    )
    ASSETS.mkdir(parents=True, exist_ok=True)
    c.image.save(ASSETS / "end-to-end-architecture.png", optimize=True)


# ---------------------------------------------------------------- service overview

INK = "#0F172A"
SOFT_INK = "#475569"
PAPER = "#FFFFFF"
CALL_COLOR = "#0F172A"
EVENT_COLOR = "#7C3AED"
SIGNAL_COLOR = "#EA580C"
GATEWAY = ("#15803D", "#DCFCE7")
SERVICE = ("#1D4ED8", "#DBEAFE")
DECISION = ("#0F766E", "#CCFBF1")
HUMAN_CARD = ("#B45309", "#FEF3C7")
AGENT = ("#7C3AED", "#EDE9FE")
DATA = ("#475569", "#E2E8F0")
EXTERNAL = ("#0369A1", "#E0F2FE")
SIGNAL = ("#EA580C", "#FFEDD5")
CARD_KINDS = {"service": SERVICE, "rule": DECISION, "human": HUMAN_CARD}
OVERVIEW_GROUPS = (
    ("Identity and tenancy", "service", tuple((name, "service") for name in ENTRY_SERVICES[2:])),
    *APPLICATION_GROUPS,
)
STREAM_NAMES = ("WINDOPS_EVENTS", "WINDOPS_TELEMETRY", "WINDOPS_AUDIT", "WINDOPS_DEAD_LETTER")


def overview_card(
    c: Canvas,
    box: tuple[float, float, float, float],
    title: str,
    sub: str | None,
    colors: tuple[str, str],
    *,
    title_size: float = 15,
    sub_size: float = 12,
) -> None:
    accent, fill = colors
    c.box(*box, fill=fill, outline=accent, width=1.5, radius=10)
    c.label(*box, title, sub, INK, SOFT_INK, title_size, sub_size)


def service_card(c: Canvas, x0: float, y0: float, width: float, name: str, kind: str) -> None:
    """Draw a service over the shared library it runs with, like a sidecar but in-process."""
    accent, fill = CARD_KINDS[kind]
    c.box(x0, y0, x0 + width, y0 + 50, fill=fill, outline=accent, width=1.25, radius=8)
    c.box(x0, y0, x0 + width, y0 + 28, fill=accent, outline=accent, radius=8)
    c.text(x0 + width / 2, y0 + 14, name, 11.5, True, PAPER, "mm")
    c.text(x0 + width / 2, y0 + 39, "breakers, signed identity", 9.5, color=SOFT_INK, anchor="mm")


def cylinder(
    c: Canvas, x0: float, y0: float, x1: float, y1: float, label: str, size: float
) -> None:
    """Draw a database, the shape the reference diagram uses for each data store."""
    s, cap = c.s, 8.0
    edge, fill = DATA
    box = (x0 * s, (y0 + cap) * s, x1 * s, (y1 - cap) * s)
    c.draw.rectangle(box, fill=fill)
    c.draw.ellipse((x0 * s, (y1 - 2 * cap) * s, x1 * s, y1 * s), fill=fill, outline=edge, width=s)
    c.draw.rectangle((x0 * s + s, (y0 + cap) * s, x1 * s - s, (y1 - cap) * s), fill=fill)
    c.draw.line((x0 * s, (y0 + cap) * s, x0 * s, (y1 - cap) * s), fill=edge, width=s)
    c.draw.line((x1 * s, (y0 + cap) * s, x1 * s, (y1 - cap) * s), fill=edge, width=s)
    c.draw.ellipse((x0 * s, y0 * s, x1 * s, (y0 + 2 * cap) * s), fill=PAPER, outline=edge, width=s)
    rows = c.wrap(label, size, x1 - x0 - 6)
    y = (y0 + y1) / 2 + 4 - (len(rows) - 1) * size * 0.6
    for row in rows:
        c.text((x0 + x1) / 2, y, row, size, color=INK, anchor="mm")
        y += size * 1.2


def pattern_line(
    c: Canvas, points: list[Point], color: str, *, dash: float, gap: float, head: bool = True
) -> None:
    """Draw a dashed or dotted line through points, ending in an arrow."""
    for start, end in itertools.pairwise(points):
        length = math.dist(start, end)
        position = 0.0
        while position < length:
            stop = min(position + dash, length)
            c.line(
                [
                    (
                        start[0] + (end[0] - start[0]) * position / length,
                        start[1] + (end[1] - start[1]) * position / length,
                    ),
                    (
                        start[0] + (end[0] - start[0]) * stop / length,
                        start[1] + (end[1] - start[1]) * stop / length,
                    ),
                ],
                color,
                1.75,
            )
            position += dash + gap
    if head:
        c.head(points[-1], points[-2], color, 8)


def dashed_frame(
    c: Canvas, box: tuple[float, float, float, float], color: str, fill: str | None = None
) -> None:
    x0, y0, x1, y1 = box
    if fill:
        c.box(x0, y0, x1, y1, fill=fill, outline=fill, radius=12)
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    pattern_line(c, corners, color, dash=7, gap=5, head=False)


def overview_label(c: Canvas, x: float, y: float, value: str, color: str = INK) -> None:
    width = c.text_width(value, 12)
    c.box(x - width / 2 - 5, y - 9, x + width / 2 + 5, y + 9, fill=PAPER, outline=PAPER, radius=4)
    c.text(x, y, value, 12, color=color, anchor="mm")


def draw_overview_services(c: Canvas, top: float) -> float:
    """Draw every service by group, and return the frame's lower edge."""
    bottom = top + 332
    dashed_frame(c, (40, top, 1240, bottom), SERVICE[0], "#F8FAFF")
    c.text(
        58,
        top + 18,
        "Kubernetes: one pod per service, calling each other over gRPC",
        14,
        True,
        SERVICE[0],
    )
    width = (1200 - 8 * 8 - 16) / 7
    x = 56.0
    for name, _, members in OVERVIEW_GROUPS:
        c.text(x + width / 2, top + 44, name, 11, True, SOFT_INK, "mm")
        y = top + 58
        for service, kind in members:
            service_card(c, x, y, width, service, kind)
            y += 54
        x += width + 8
    return bottom


def draw_overview_data(c: Canvas, top: float) -> float:
    """Draw where data lives: one server holding a database per service, and the other stores."""
    bottom = top + 214
    dashed_frame(c, (40, top, 1012, bottom), DATA[0], "#F8FAFC")
    c.text(
        58,
        top + 18,
        "One PostgreSQL pod: 20 separate databases, each with its own login",
        14,
        True,
        INK,
    )
    names = [database_name(service).removeprefix("windops_") for service in SERVICE_NAMESPACES]
    per_row = 10
    width, gap = 88.0, 7.0
    for index, name in enumerate(names):
        x0 = 54 + (index % per_row) * (width + gap)
        y0 = top + 36 + (index // per_row) * 86
        cylinder(c, x0, y0, x0 + width, y0 + 78, name.replace("_", " "), 10.5)
    for x0, name, note in (
        (1032, "Redis", "cache, rate limits"),
        (1140, "Qdrant", "vector search"),
    ):
        cylinder(c, x0, top + 16, x0 + 100, top + 150, name, 13)
        for index, row in enumerate(c.wrap(note, 11, 100)):
            c.text(x0 + 50, top + 166 + index * 14, row, 11, color=SOFT_INK, anchor="mm")
    return bottom


def draw_overview_stream(c: Canvas, top: float) -> float:
    """Draw JetStream as a tube, with its streams as the sections."""
    edge, fill = "#7C3AED", "#F3E8FF"
    c.box(40, top, 1240, top + 92, fill=fill, outline=edge, width=1.5, radius=24)
    c.text(
        640,
        top + 19,
        "NATS JetStream: durable events, through each service's outbox",
        14,
        True,
        EVENT_COLOR,
        "mm",
    )
    width = (1200 - 40 - 3 * 10) / 4
    x = 60.0
    for name in STREAM_NAMES:
        c.box(x, top + 38, x + width, top + 78, fill=PAPER, outline=edge, radius=6)
        c.text(x + width / 2, top + 58, name, 12, color=EVENT_COLOR, anchor="mm")
        x += width + 10
    return top + 92


def draw_overview_observability(c: Canvas, top: float) -> float:
    bottom = top + 122
    dashed_frame(c, (40, top, 1240, bottom), SIGNAL[0], "#FFF7ED")
    c.text(58, top + 18, "Observability stack, for every workload", 14, True, SIGNAL[0])
    tools = (
        ("Logs", "Fluent Bit, Loki"),
        ("Metrics", "Prometheus, Alertmanager"),
        ("Traces", "OpenTelemetry, Tempo"),
        ("Dashboards", "Grafana"),
    )
    width = (1200 - 32 - 3 * 14) / 4
    x = 56.0
    for name, note in tools:
        overview_card(c, (x, top + 38, x + width, top + 106), name, note, SIGNAL)
        x += width + 14
    return bottom


def draw_overview_legend(c: Canvas, x: float, top: float) -> None:
    c.box(x, top, 1560, top + 168, fill=PAPER, outline="#CBD5E1", radius=10)
    c.text((x + 1560) / 2, top + 20, "Legend", 14, True, INK, "mm")
    rows = (
        ("call", "Call: the caller waits", "HTTPS or gRPC, with a breaker"),
        ("event", "Event: the sender does not wait", "JetStream, through an outbox"),
        ("signal", "Logs, metrics and traces", "From every workload"),
    )
    y = top + 50
    for kind, title, note in rows:
        start, end = (x + 14, y + 8), (x + 58, y + 8)
        if kind == "call":
            c.arrow([start, end], CALL_COLOR, 1.75)
        elif kind == "event":
            pattern_line(c, [start, end], EVENT_COLOR, dash=7, gap=5)
        else:
            pattern_line(c, [start, end], SIGNAL_COLOR, dash=2, gap=4)
        c.text(x + 68, y, title, 12, True, INK)
        c.text(x + 68, y + 17, note, 11, color=SOFT_INK)
        y += 40


def build_service_overview() -> None:
    """Draw every service, its data, events and signals, on one light page for quick reading."""
    c = Canvas(1600, 1244, background=PAPER)
    c.text(40, 32, "WindOps AI: services, data and events", 28, True, INK)
    c.text(40, 62, "Target design from SRS v1.1, built in phases.", 15, color=SOFT_INK)

    # Coming in
    overview_card(c, (250, 86, 470, 134), "React frontend", "operators and approvers", HUMAN_CARD)
    overview_card(c, (810, 86, 1030, 134), "Telemetry simulator", "synthetic SCADA and CMS", DATA)
    overview_card(
        c,
        (40, 172, 1240, 222),
        "API Gateway (Apache APISIX)",
        "the only way in: TLS, rate limits and token checks",
        GATEWAY,
    )
    c.arrow([(360, 134), (360, 172)], CALL_COLOR, 1.75)
    overview_label(c, 402, 153, "HTTPS")
    c.arrow([(920, 134), (920, 172)], CALL_COLOR, 1.75)
    overview_label(c, 1000, 153, "device route to Telemetry")

    # Identity, outside the request path
    overview_card(c, (1290, 172, 1560, 222), "Keycloak", "login, MFA and tokens", EXTERNAL)
    c.arrow([(1240, 197), (1290, 197)], CALL_COLOR, 1.75)
    overview_label(c, 1265, 161, "OIDC")

    # Web BFF and agents
    overview_card(
        c,
        (40, 258, 560, 306),
        "Web BFF",
        "REST and SSE for the frontend, no business rules",
        GATEWAY,
    )
    overview_card(
        c, (620, 258, 900, 306), AGENT_SERVICE, "Supervisor and 5 specialist agents", AGENT
    )
    overview_card(
        c, (960, 258, 1240, 306), "MCP servers", "an agent's only route to services", AGENT
    )
    c.arrow([(300, 222), (300, 258)], CALL_COLOR, 1.75)
    c.arrow([(900, 282), (960, 282)], CALL_COLOR, 1.75)
    c.arrow([(300, 306), (300, 344)], CALL_COLOR, 1.75)
    overview_label(c, 340, 325, "gRPC")
    c.arrow([(1100, 306), (1100, 344)], CALL_COLOR, 1.75)
    overview_label(c, 1140, 325, "gRPC")

    services_bottom = draw_overview_services(c, 344)

    # External providers
    c.text(1425, 268, "External providers", 14, True, EXTERNAL[0], "mm")
    providers = (
        ("Claude API", "Agent Runtime"),
        ("Tavily", "External Intelligence"),
        ("Open-Meteo", "Weather"),
        ("OpenAI", "embeddings, Knowledge"),
        ("LangSmith", "traces, Sandbox"),
        ("Email provider", "Notification"),
    )
    y = 284.0
    for name, note in providers:
        overview_card(c, (1290, y, 1560, y + 54), name, note, EXTERNAL, title_size=13, sub_size=11)
        y += 62
    c.arrow([(1240, 510), (1290, 510)], CALL_COLOR, 1.75)
    overview_label(c, 1265, 492, "egress")

    # Events, then data
    stream_top = services_bottom + 34
    stream_bottom = draw_overview_stream(c, stream_top)
    pattern_line(c, [(500, services_bottom), (500, stream_top)], EVENT_COLOR, dash=7, gap=5)
    pattern_line(c, [(780, stream_top), (780, services_bottom)], EVENT_COLOR, dash=7, gap=5)
    overview_label(c, 640, services_bottom + 17, "publish and consume", EVENT_COLOR)

    data_top = stream_bottom + 42
    data_bottom = draw_overview_data(c, data_top)
    # Past the stream, because services reach data directly, not through it.
    c.arrow(
        [(22, services_bottom - 40), (22, data_top + 40), (40, data_top + 40)], CALL_COLOR, 1.75
    )
    c.line([(40, services_bottom - 40), (22, services_bottom - 40)], CALL_COLOR, 1.75)
    c.arrow(
        [(1256, services_bottom - 40), (1256, data_top + 90), (1240, data_top + 90)],
        CALL_COLOR,
        1.75,
    )
    c.line([(1240, services_bottom - 40), (1256, services_bottom - 40)], CALL_COLOR, 1.75)
    c.text(1268, services_bottom + 60, "A service reaches only", 11.5, color=SOFT_INK)
    c.text(1268, services_bottom + 76, "its own database and", 11.5, color=SOFT_INK)
    c.text(1268, services_bottom + 92, "its own Redis keys.", 11.5, color=SOFT_INK)

    observability_top = data_bottom + 38
    draw_overview_observability(c, observability_top)
    pattern_line(c, [(640, data_bottom), (640, observability_top)], SIGNAL_COLOR, dash=2, gap=4)
    overview_label(c, 720, data_bottom + 19, "logs, metrics, traces", SIGNAL_COLOR)

    draw_overview_legend(c, 1290, observability_top - 46)
    ASSETS.mkdir(parents=True, exist_ok=True)
    c.image.save(ASSETS / "service-overview.png", optimize=True)


# --------------------------------------------------------------- orchestration

W, H = 1000, 640
FPS = 10
AI = KINDS["ai"][0]
RULE = KINDS["rule"][0]
HUMAN = KINDS["human"][0]

ROWS = [127, 183, 239, 295, 351]
ROW_NAMES = ["scada", "reliability", "safety", "impact", "planner"]

NODES: dict[str, tuple[float, float, float, float, str, str | None, str]] = {
    "telemetry": (20, 92, 190, 138, "Telemetry", "gearbox temp, vibration", "service"),
    "fault": (20, 153, 190, 199, "Fault Detection", "versioned rules", "rule"),
    "case": (20, 215, 190, 263, "Investigation", "opens the case", "service"),
    "snapshot": (20, 300, 190, 348, "Evidence snapshot", "immutable", "rule"),
    "supervisor": (236, 199, 356, 279, "Supervisor", "plans, delegates", "ai"),
    "result": (20, 412, 196, 482, "Structured result", "schema validated", "rule"),
    "policy": (216, 412, 392, 482, "Policy Service", "pass, block or escalate", "rule"),
    "draft": (412, 412, 588, 482, "Draft work order", "agents reach draft only", "ai"),
    "approver": (608, 412, 784, 482, "Human approver", "approves or rejects", "human"),
    "record": (804, 412, 980, 482, "Audit and notify", "decision is traceable", "rule"),
}
for name, y, spec, mcp, service in zip(
    ROW_NAMES,
    ROWS,
    [
        ("SCADA Analyst", "reads trends and alarms"),
        ("Reliability Engineer", "weighs failure modes"),
        ("Safety and Procedure Researcher", None),
        ("Production Impact", "explains the figures"),
        ("Maintenance Planner", "prepares the proposal"),
    ],
    ["WindOps MCP", "Documentation MCP", "Tavily, Weather MCP", "WindOps MCP", "WindOps MCP"],
    [
        ("Telemetry, Alarm", "service"),
        ("Knowledge", "service"),
        ("External Intel, Weather", "service"),
        ("Production Impact", "rule"),
        ("Maintenance, Inventory", "service"),
    ],
    strict=True,
):
    NODES[f"{name}_agent"] = (420, y - 24, 596, y + 24, spec[0], spec[1], "ai")
    NODES[f"{name}_mcp"] = (636, y - 24, 796, y + 24, mcp, None, "service")
    NODES[f"{name}_svc"] = (820, y - 24, 980, y + 24, service[0], None, service[1])

PATHS: dict[str, list[Point]] = {
    "telemetry_fault": [(105, 138), (105, 153)],
    "fault_case": [(105, 199), (105, 215)],
    "case_snapshot": [(105, 263), (105, 300)],
    "case_supervisor": [(190, 239), (236, 239)],
    "supervisor_result": [(296, 279), (296, 400), (108, 400), (108, 412)],
    "result_policy": [(196, 447), (216, 447)],
    "policy_draft": [(392, 447), (412, 447)],
    "draft_approver": [(588, 447), (608, 447)],
    "approver_record": [(784, 447), (804, 447)],
}
for name, y in zip(ROW_NAMES, ROWS, strict=True):
    PATHS[f"delegate_{name}"] = [(356, 239), (388, 239), (388, y), (420, y)]
    PATHS[f"tool_{name}"] = [(596, y), (636, y)]
    PATHS[f"grpc_{name}"] = [(796, y), (820, y)]
    PATHS[f"back_row_{name}"] = [(820, y), (388, y), (388, 239), (356, 239)]

ALL_ROWS = ROW_NAMES[:4]


def row_nodes(name: str) -> list[str]:
    return [f"{name}_agent", f"{name}_mcp", f"{name}_svc"]


# Each pulse is (path, start, end, color), where start and end are fractions of the step.
STEPS: list[dict[str, Any]] = [
    {
        "title": "A deterministic rule detects the fault",
        "detail": "A synthetic gearbox temperature and vibration abnormality trips a "
        "versioned rule. "
        "No LLM assigns severity.",
        "accent": RULE,
        "dur": 2.8,
        "active": ["telemetry", "fault"],
        "pulses": [("telemetry_fault", 0.1, 0.6, RULE)],
    },
    {
        "title": "A case opens with frozen evidence",
        "detail": "The Investigation Service opens a case and records an immutable "
        "evidence snapshot "
        "with its data quality.",
        "accent": RULE,
        "dur": 2.8,
        "active": ["case", "snapshot"],
        "pulses": [("fault_case", 0.05, 0.4, RULE), ("case_snapshot", 0.45, 0.85, RULE)],
    },
    {
        "title": "The Supervisor plans and delegates",
        "detail": "It creates a bounded plan and assigns specialists. Tool call, retry, "
        "token and cost "
        "budgets are enforced.",
        "accent": AI,
        "dur": 2.8,
        "active": ["supervisor"],
        "pulses": [("case_supervisor", 0.05, 0.4, AI)]
        + [(f"delegate_{n}", 0.4, 0.95, AI) for n in ROW_NAMES[:4]],
    },
    {
        "title": "Specialists gather cited evidence",
        "detail": "Each one reaches services only through allowlisted MCP tools. "
        "Retrieved content is treated as untrusted data.",
        "accent": AI,
        "dur": 6.0,
        "active": [n for name in ALL_ROWS for n in row_nodes(name)],
        "pulses": [
            pulse
            for i, name in enumerate(ALL_ROWS)
            for pulse in (
                (f"tool_{name}", 0.0 + i * 0.05, 0.1 + i * 0.05, AI),
                (f"grpc_{name}", 0.1 + i * 0.05, 0.2 + i * 0.05, AI),
                ("back_row_" + name, 0.3 + i * 0.1, 0.65 + i * 0.1, RULE),
            )
        ],
    },
    {
        "title": "The Maintenance Planner prepares a proposal",
        "detail": "Only schema-validated fields with source references reach it. "
        "It never reads raw web content.",
        "accent": AI,
        "dur": 3.2,
        "active": row_nodes("planner"),
        "pulses": [
            ("delegate_planner", 0.0, 0.3, AI),
            ("tool_planner", 0.3, 0.4, AI),
            ("grpc_planner", 0.4, 0.5, AI),
            ("back_row_planner", 0.55, 0.95, RULE),
        ],
    },
    {
        "title": "Findings are aggregated and validated",
        "detail": "The Supervisor combines findings, contradictions and confidence. "
        "The output must pass schema validation, with at most two repairs.",
        "accent": AI,
        "dur": 3.0,
        "active": ["supervisor", "result"],
        "pulses": [("supervisor_result", 0.15, 0.85, AI)],
    },
    {
        "title": "Policy checks run outside the LLM",
        "detail": "The Policy Service returns pass, block or escalate. "
        "A missing or stale required input fails closed.",
        "accent": RULE,
        "dur": 3.0,
        "active": ["policy"],
        "pulses": [("result_policy", 0.1, 0.6, RULE)],
    },
    {
        "title": "A draft is prepared and the run stops",
        "detail": "The checkpoint is saved, the case moves to pending_review and the "
        "active run ends. "
        "Nothing proceeds without a human decision.",
        "accent": AI,
        "dur": 3.2,
        "active": ["draft"],
        "pulses": [("policy_draft", 0.1, 0.5, AI)],
        "pending": True,
    },
    {
        "title": "An authorized human decides",
        "detail": "An eligible reviewer weighs the evidence and the policy result. "
        "No agent identity can approve.",
        "accent": HUMAN,
        "dur": 3.4,
        "active": ["approver"],
        "pulses": [("draft_approver", 0.1, 0.45, HUMAN)],
        "pending": True,
    },
    {
        "title": "The run resumes and everything is recorded",
        "detail": "A durable event resumes the run, users are notified and audit keeps "
        "the references. "
        "The system never issues turbine commands.",
        "accent": RULE,
        "dur": 4.6,
        "active": ["record"],
        "pulses": [("approver_record", 0.05, 0.3, RULE)],
    },
]

# An edge is drawn lit once this step has started.
EDGE_STEPS = {
    "telemetry_fault": 0,
    "fault_case": 1,
    "case_snapshot": 1,
    "case_supervisor": 2,
    "supervisor_result": 5,
    "result_policy": 6,
    "policy_draft": 7,
    "draft_approver": 8,
    "approver_record": 9,
}
for name in ALL_ROWS:
    EDGE_STEPS[f"delegate_{name}"] = 2
    EDGE_STEPS[f"tool_{name}"] = 3
    EDGE_STEPS[f"grpc_{name}"] = 3
EDGE_STEPS.update({"delegate_planner": 4, "tool_planner": 4, "grpc_planner": 4})
EDGE_COLORS = {"draft_approver": HUMAN}


def point_along(path: list[Point], fraction: float) -> Point:
    lengths = [math.dist(a, b) for a, b in itertools.pairwise(path)]
    target = sum(lengths) * fraction
    for (a, b), length in zip(itertools.pairwise(path), lengths, strict=True):
        if target <= length and length > 0:
            t = target / length
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        target -= length
    return path[-1]


def draw_node(c: Canvas, key: str, state: str, glow: float) -> None:
    x0, y0, x1, y1, title, sub, kind = NODES[key]
    accent, fill = KINDS[kind]
    if state == "idle":
        c.box(x0, y0, x1, y1, fill=PANEL, outline=LINE)
        c.label(x0, y0, x1, y1, title, sub, mix(TEXT, BG, 0.35), mix(MUTED, BG, 0.35), 14, 12)
        return
    if state == "active":
        pad = 2 + glow
        c.box(
            x0 - pad,
            y0 - pad,
            x1 + pad,
            y1 + pad,
            fill=mix(accent, BG, 0.72),
            outline=mix(accent, BG, 0.45),
            radius=10,
        )
    c.box(x0, y0, x1, y1, fill=fill, outline=accent, width=1.75 if state == "active" else 1)
    c.label(x0, y0, x1, y1, title, sub, TEXT, MUTED, 14, 12)


def draw_frame(step_index: int, progress: float, clock: float) -> Image.Image:
    c = Canvas(W, H)
    step = STEPS[step_index]

    c.text(20, 28, "From turbine fault to human decision", 22, True)
    x: float = 980
    for kind, name in reversed([("ai", "AI agent"), ("rule", "Deterministic"), ("human", "Human")]):
        accent, fill = KINDS[kind]
        x -= c.text_width(name, 13)
        c.text(x, 28, name, 13, color=MUTED)
        x -= 21
        c.box(x, 20, x + 15, 36, fill=fill, outline=accent, radius=4)
        x -= 18

    segment = (W - 40 - 9 * 4) / 10
    for i in range(10):
        x0 = 20 + i * (segment + 4)
        fraction = 1 if i < step_index else (progress if i == step_index else 0)
        c.box(x0, 50, x0 + segment, 55, fill=LINE, outline=LINE, radius=2)
        if fraction:
            c.box(x0, 50, x0 + segment * fraction, 55, fill=TEXT, outline=TEXT, radius=2)

    for x0, label in [
        (20, "TRIGGER"),
        (420, "SPECIALISTS"),
        (636, "MCP TOOLS"),
        (820, "SERVICES, gRPC"),
    ]:
        c.text(x0, 82, label, 11.5, True, MUTED)
    c.box(222, 72, 606, 386, fill=None, outline=mix(AI, BG, 0.55), radius=10)
    c.text(236, 88, "AGENT RUNTIME", 11.5, True, mix(AI, TEXT, 0.25))
    c.text(236, 104, "LangGraph, Claude", 11.5, color=MUTED)

    for key, path in PATHS.items():
        if key in EDGE_STEPS:
            lit = step_index >= EDGE_STEPS[key]
            color = EDGE_COLORS.get(key, MUTED) if lit else LINE
            c.line(path, color, 1.6 if lit else 1.2)
            c.head(path[-1], path[-2], color, 6)

    for path_key, start, end, color in step["pulses"]:
        if start <= progress <= end:
            fraction = (progress - start) / (end - start)
            path = PATHS[path_key]
            for trail, radius in ((0.06, 3), (0.03, 4)):
                x, y = point_along(path, max(0, fraction - trail))
                c.dot(x, y, radius, mix(color, BG, 0.55))
            x, y = point_along(path, fraction)
            c.dot(x, y, 6, color)
            c.dot(x, y, 2.5, "#FFFFFF")

    active = set(step["active"])
    seen = set()
    for earlier in STEPS[: step_index + 1]:
        seen.update(earlier["active"])
    glow = 0.5 + 0.5 * math.sin(clock * 5)
    for key in NODES:
        state = "active" if key in active else ("done" if key in seen else "idle")
        draw_node(c, key, state, glow)

    if step.get("pending"):
        c.box(604, 386, 788, 408, fill=KINDS["human"][1], outline=HUMAN, radius=11)
        c.text(696, 397, "case: pending_review", 12, True, TEXT, "mm")

    accent = step["accent"]
    c.box(20, 498, 980, 596, fill=PANEL)
    c.box(20, 498, 26, 596, fill=accent, outline=accent, radius=3)
    c.text(44, 523, f"{step_index + 1}", 22, True, accent, "mm")
    c.text(66, 523, step["title"], 19, True)
    rows = c.wrap(step["detail"], 15, 890)
    if len(rows) > 2:
        print(f"WARNING: caption is too long: {step['title']!r}")
    for i, row in enumerate(rows):
        c.text(66, 551 + i * 21, row, 15, color=mix(TEXT, MUTED, 0.5))
    c.text(
        500,
        618,
        "Illustration of the SRS v1.1 design, using a synthetic scenario. "
        "Not a recording of a running system.",
        12.5,
        color=MUTED,
        anchor="mm",
    )

    return c.image.resize((W, H), Image.Resampling.LANCZOS)


def build_animation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        count, clock = 0, 0.0
        for index, step in enumerate(STEPS):
            frames = round(step["dur"] * FPS)
            for i in range(frames):
                progress = (i + 1) / frames
                draw_frame(index, progress, clock).save(f"{tmp}/{count:04d}.png")
                count += 1
                clock += 1 / FPS
        ASSETS.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-framerate",
                str(FPS),
                "-i",
                f"{tmp}/%04d.png",
                "-filter_complex",
                "split[a][b];[a]palettegen=max_colors=96:stats_mode=full[p];"
                "[b][p]paletteuse=dither=none:diff_mode=rectangle",
                "-loop",
                "0",
                str(ASSETS / "agent-orchestration.gif"),
            ],
            check=True,
        )
        print(f"Encoded {count} frames at {FPS} fps")


if __name__ == "__main__":
    build_architecture()
    build_security_layers()
    build_end_to_end()
    build_service_overview()
    build_animation()
