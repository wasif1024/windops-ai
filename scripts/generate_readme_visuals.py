#!/usr/bin/env python3
"""Generate the README architecture diagram and agent orchestration animation.

Both visuals show the target design in docs/srs.md (sections 5, 6, 7 and 8).
Update this script when that design changes, then run it from any directory:

    uv run python scripts/generate_readme_visuals.py

Requires Pillow and ffmpeg. Writes to docs/assets/.
"""

import itertools
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from PIL import Image, ImageDraw, ImageFont

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

    def __init__(self, width: int, height: int, scale: int = 2) -> None:
        self.width, self.height, self.s = width, height, scale
        self.image = Image.new("RGB", (width * scale, height * scale), BG)
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
    c.text(
        40, 66, "Target design from SRS v1.1. Implementation starts with Phase 1.", 16, color=MUTED
    )

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
    c.frame(40, 840, 660, 950, "Data: each service owns its schema")
    stores = [
        ("PostgreSQL", "system of record"),
        ("TimescaleDB", "telemetry"),
        ("Redis", "cache, rate limits"),
        ("Qdrant", "vector retrieval"),
    ]
    for i, (name, note) in enumerate(stores):
        x0 = 52 + i * 150
        c.node(x0, 868, x0 + 144, 940, name, note, title_size=14)

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
        "Observability: OpenTelemetry, Prometheus, Grafana, Loki, Fluent Bit and Alertmanager. "
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
    build_animation()
