#!/usr/bin/env python3
"""Generate the README's platform architecture diagram and agent orchestration animation.

These visuals show the target design in docs/srs.md (sections 5, 6, 7 and 8).
Update this script when that design changes, then run it from any directory:

    uv run python -m scripts.generate.readme_visuals

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

Point = tuple[float, float]

ASSETS = Path(__file__).resolve().parents[2] / "docs" / "assets"

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


# ------------------------------------------------------------ platform architecture

GREEN = "#34D399"
AMBER = "#FBBF24"
RED = "#F87171"
EVENT = "#C4B5FD"
SIGNAL = "#FB923C"
TRACKER = ASSETS.parent / "phases" / "README.md"
SERVICES_DIRECTORY = ASSETS.parents[1] / "services"


@dataclass(frozen=True)
class Control:
    text: str
    module: str | None = None  # The tracker modules that deliver it, or None when in place.


# Security on each hop, and around them. The tests hold every cited module to the tracker, so a
# control cannot stay "planned" once its modules are done.
HOP_CONTROLS = (
    (
        "Coming in",
        (
            Control("The gateway is the only way in, over TLS, and nothing else is exposed"),
            Control("Sign-in through Keycloak (OIDC), with MFA for administrators"),
            Control("Token checks, CORS allowlists and SRS rate limits on every route"),
            Control("Each device has its own credential, for one tenant and turbine"),
            Control("On Azure, a WAF in Prevention mode in front of the gateway", "M4A.6"),
        ),
    ),
    (
        "Between services",
        (
            Control("Mutual TLS, a signed identity and a deadline on every call"),
            Control("The tenant comes from the verified identity, never a request field"),
            Control("Permissions denied by default, decided by the Authorization Service"),
            Control("Nobody can approve their own request", "M10.2"),
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
            Control("Each service has its own database and login"),
            Control("Forced row-level security isolates every tenant"),
            Control("Audit records are append-only and tamper-evident"),
            Control("Outbound traffic only to named endpoints", "M13.3"),
        ),
    ),
    (
        "Cluster and delivery",
        (
            Control("Non-root pods, read-only filesystems, memory limits and probes"),
            Control("Deny all traffic between namespaces by default", "M13.3"),
            Control("Every change passes lint, types, tests, CodeQL and secret scans"),
            Control("On Azure, no stored secrets: Workload ID and Key Vault", "M4A.6"),
            Control("Signed container images", "M14.2"),
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
OVERVIEW_GROUPS = (
    ("Identity and tenancy", "service", tuple((name, "service") for name in ENTRY_SERVICES[2:])),
    *APPLICATION_GROUPS,
)
# Each stream locally, and its Service Bus topic on Azure (ADR 0006).
STREAM_NAMES = (
    "WINDOPS_EVENTS / windops-events",
    "WINDOPS_TELEMETRY / windops-telemetry",
    "WINDOPS_AUDIT / windops-audit",
    "WINDOPS_DEAD_LETTER / dead-letter queues",
)
# What M4A.6 creates, from docs/azure-plan.md.
AZURE_RESOURCES = (
    ("Application Gateway", "WAF v2 in front of APISIX"),
    ("AKS", "Azure CNI Overlay, powered by Cilium"),
    ("Azure Service Bus", "topics, subscriptions, dead letters"),
    ("Azure Database for PostgreSQL", "Entra sign-in, private access"),
    ("Key Vault", "secrets through the CSI driver"),
    ("Entra Workload ID", "an identity per workload"),
    ("Container Registry", "pulled with the cluster's identity"),
    ("Azure Monitor", "Insights, logs, Prometheus, Grafana"),
)
AZURE_PANEL_HEIGHT = 138.0
SPECIALISTS = (
    "SCADA Analyst",
    "Reliability Engineer",
    "Safety and Procedure Researcher",
    "Production Impact",
    "Maintenance Planner",
)
MCP_SERVERS = (
    ("WindOps MCP", "tenant-scoped, draft only"),
    ("Documentation MCP", "document permissions"),
    ("Tavily MCP", "external web research"),
    ("Weather MCP", "weather only"),
)
PROVIDERS = (
    ("Claude API", "agents"),
    ("OpenAI", "embeddings"),
    ("Tavily", "web research"),
    ("Open-Meteo", "weather"),
    ("LangSmith", "traces, evals"),
    ("Email", "notifications"),
)
BREAKER_OUTCOMES = (
    ("Refuse", RED, "Authorization, Policy, Production Impact, Keycloak, PostgreSQL"),
    ("Pause", AMBER, "Claude API, Knowledge, Telemetry, Approval, forecasts"),
    ("Continue", GREEN, "Tavily, Sandbox, Audit, Notification, events"),
)
RUNTIMES = (
    ("Local", "Minikube with Cilium: 6 CPUs, 10 GB (ADR 0001)"),
    ("CI", "Checks, scans and image builds"),
    (
        "Azure",
        "AKS in Sweden Central, with managed data, events and monitoring (ADR 0006)",
    ),
)
# From docs/azure-plan.md: compute runs only while it is used.
AZURE_COSTS = (
    ("Idle", "data only, between demos", "$20"),
    ("Demo", "compute for 60 hours, the plan", "$85"),
    ("Always on", "everything, all month", "$778"),
)
GUARDRAILS = (
    "Agents recommend and draft. Policy, Approval and people decide.",
    "50 tool calls, 3 retries and 2 repairs per run by default.",
    "Token and cost budgets per tenant.",
    "Each run pins its graph, prompt, model and tool versions.",
)


def all_controls() -> list[Control]:
    return [control for _, controls in HOP_CONTROLS for control in controls]


def is_running(name: str) -> bool:
    """A service runs today once its directory holds an image to build, or its deployment."""
    slug = name.lower().replace(" ", "-")
    return any(
        (SERVICES_DIRECTORY / directory / marker).exists()
        for directory in (slug, f"{slug}-service")
        for marker in ("Dockerfile", "deploy/kustomization.yaml")
    )


def read_phase_progress(tracker: Path = TRACKER) -> list[tuple[str, str, int, int]]:
    """Return each phase's number, name, finished modules and total modules from the tracker."""
    text = tracker.read_text()
    phases = []
    # A phase is a number, or a number and a letter for one added between two, such as 4A.
    for match in re.finditer(r"^## Phase (\d+[A-Z]?): (.+)$", text, re.M):
        following = text[match.end() :]
        next_heading = following.find("\n## ")
        section = following if next_heading == -1 else following[:next_heading]
        done = len(re.findall(r"^- \[x\] ", section, re.M))
        total = done + len(re.findall(r"^- \[ \] ", section, re.M))
        phases.append((match.group(1), match.group(2), done, total))
    return phases


def draw_control_marker(c: Canvas, x: float, y: float, planned: bool) -> None:
    if planned:
        c.dot(x, y, 5, AMBER)
        c.dot(x, y, 3, PANEL)
    else:
        c.dot(x, y, 5, GREEN)


def draw_controls(
    c: Canvas, x: float, y: float, controls: tuple[Control, ...], width: float, size: float = 13
) -> float:
    """Draw each control with its marker, and return the y below them."""
    for control in controls:
        suffix = f"  ({control.module})" if control.module else ""
        planned = control.module is not None
        rows = c.wrap(control.text + suffix, size, width - 20)
        draw_control_marker(c, x + 5, y + 8, planned)
        for index, row in enumerate(rows):
            c.text(x + 18, y + 8 + index * 17, row, size, color=MUTED if planned else TEXT)
        y += len(rows) * 17 + 3
    return y


def dashed(c: Canvas, points: list[Point], color: str, dash: float = 6, gap: float = 4) -> None:
    """A dashed line through points, ending in an arrow: an event, which nobody waits on."""
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
                1.5,
            )
            position += dash + gap
    c.head(points[-1], points[-2], color)


def edge_label(c: Canvas, x: float, y: float, value: str, color: str = MUTED) -> None:
    width = c.text_width(value, 12)
    c.box(x - width / 2 - 5, y - 9, x + width / 2 + 5, y + 9, fill=BG, outline=BG, radius=4)
    c.text(x, y, value, 12, color=color, anchor="mm")


def namespace(c: Canvas, x0: float, y0: float, x1: float, y1: float, name: str, note: str) -> None:
    """A Kubernetes namespace: a quiet frame, named in its corner."""
    c.box(x0, y0, x1, y1, fill=PANEL, outline=LINE, radius=10)
    c.text(x0 + 14, y0 + 16, name, 12.5, True, KINDS["external"][0])
    if note:
        c.text(x0 + 22 + c.text_width(name, 12.5, True), y0 + 16, note, 12, color=MUTED)


def draw_service(
    c: Canvas, x0: float, y0: float, x1: float, y1: float, name: str, kind: str
) -> None:
    """A service, with a dot when it runs today."""
    size = 12.5 if c.text_width(name, 12.5, True) <= x1 - x0 - 26 else 11
    c.node(x0, y0, x1, y1, name, None, kind, title_size=size)
    if is_running(name):
        c.dot(x1 - 9, y0 + 9, 3.5, GREEN)


def draw_edge(c: Canvas) -> None:
    """People and devices, and the namespace that lets them in."""
    c.node(
        52, 112, 232, 158, "Operators and approvers", "8 roles, scoped access", "human", 12.5, 11.5
    )
    c.node(256, 112, 420, 158, "React frontend", "tenant-branded, responsive", "service", 13, 11.5)
    c.node(
        830, 112, 1060, 158, "Telemetry simulator", "a credential per turbine", "service", 13, 11.5
    )
    # The simulator is not a service, so it runs once its workload is deployable.
    if (SERVICES_DIRECTORY.parent / "simulator" / "deploy" / "kustomization.yaml").exists():
        c.dot(1051, 121, 3.5, GREEN)
    c.arrow([(232, 135), (256, 135)])

    namespace(c, 40, 186, 1080, 336, "windops-system", "")
    gateway, bff = (60, 214, 380, 266), (440, 214, 640, 266)
    c.node(
        *gateway, "API Gateway", "Apache APISIX: the only way in. On Azure, behind a WAF",
        "service", 14, 11.5,
    )  # fmt: skip
    c.node(60, 280, 380, 322, "Keycloak", "sign-in, MFA and tokens", "external", 13, 11.5)
    c.node(*bff, "Web BFF", "REST and SSE only", "service", 14, 11.5)
    for name, box in (("API Gateway", gateway), ("Web BFF", bff)):
        if is_running(name):
            c.dot(box[2] - 9, box[1] + 9, 3.5, GREEN)
    c.text(876, 222, "Identity and tenancy", 12, True, MUTED, "mm")
    for index, name in enumerate(ENTRY_SERVICES[2:]):
        x0 = 690 + index * 128
        draw_service(c, x0, 234, x0 + 120, 266, name, "service")

    c.arrow([(300, 158), (300, 214)])
    edge_label(c, 300, 188, "HTTPS")
    c.arrow([(945, 158), (945, 176), (360, 176), (360, 214)], SIGNAL)
    edge_label(c, 650, 176, "device samples, HTTPS to /ingest", SIGNAL)
    c.arrow([(140, 266), (140, 280)], both=True)
    edge_label(c, 186, 273, "OIDC")
    c.arrow([(380, 240), (440, 240)])
    c.arrow([(640, 250), (690, 250)])
    edge_label(c, 665, 232, "gRPC")


def draw_agents(c: Canvas) -> None:
    namespace(c, 40, 356, 640, 520, "windops-application", "Agent Runtime: LangGraph and Claude")
    ai = KINDS["ai"][0]
    c.node(240, 380, 440, 420, "Supervisor", "plans, delegates, synthesizes", "ai", 13.5, 11.5)
    width, gap = 108.0, 6.0
    for index, name in enumerate(SPECIALISTS):
        x0 = 56 + index * (width + gap)
        c.line([(340, 420), (340, 434), (x0 + width / 2, 434), (x0 + width / 2, 448)], ai)
        c.node(x0, 448, x0 + width, 506, name, None, "ai", title_size=12)

    namespace(c, 670, 356, 1080, 520, "windops-mcp", "an agent's only route to services")
    for index, (name, note) in enumerate(MCP_SERVERS):
        x0 = 686 + (index % 2) * 194
        y0 = 384 + (index // 2) * 66
        c.node(x0, y0, x0 + 184, y0 + 56, name, note, "service", 13, 11.5)
    c.arrow([(640, 438), (670, 438)])
    edge_label(c, 655, 424, "tools")


def draw_services(c: Canvas) -> tuple[float, float]:
    top, bottom = 548, 770
    namespace(c, 40, top, 1080, bottom, "windops-application", "one pod per service, gRPC")
    width, gap = 166.0, 6.0
    for index, (group, kind, members) in enumerate(APPLICATION_GROUPS):
        x0 = 56 + index * (width + gap)
        c.box(
            x0, top + 30, x0 + width, bottom - 12, fill=BG,
            outline=KINDS[kind][0] if kind != "service" else LINE, radius=8,
        )  # fmt: skip
        c.text(x0 + width / 2, top + 44, group, 12, True, MUTED, "mm")
        for row, (name, member_kind) in enumerate(members):
            y0 = top + 58 + row * 30
            draw_service(c, x0 + 7, y0, x0 + width - 7, y0 + 25, name, member_kind)
    # Calls from Web BFF, and from MCP servers. Both go through each caller's circuit breaker.
    c.arrow([(540, 266), (540, 346), (1092, 346), (1092, top + 40), (1080, top + 40)])
    edge_label(c, 820, 346, "gRPC to domain services: mTLS, signed identity, breakers")
    c.arrow([(875, 520), (875, top)])
    edge_label(c, 875, 534, "gRPC")
    # Queued agent runs, from Investigation to the Agent Runtime.
    dashed(c, [(48, 702), (30, 702), (30, 440), (40, 440)], EVENT)
    edge_label(c, 68, 534, "queued runs", EVENT)
    return top, bottom


def draw_events_and_data(c: Canvas, services_bottom: float) -> None:
    y0, y1 = services_bottom + 24, services_bottom + 76
    c.box(40, y0, 1080, y1, fill=mix(BG, EVENT, 0.12), outline=EVENT, radius=10)
    title = "NATS JetStream locally, Azure Service Bus on Azure"
    c.text(56, y0 + 16, title, 13, True, EVENT)
    c.text(
        56 + c.text_width(title, 13, True) + 10, y0 + 16,
        "durable events from each service's outbox: the sender never waits",
        12, color=MUTED,
    )  # fmt: skip
    width = (1040 - 3 * 8 - 24) / 4
    for index, name in enumerate(STREAM_NAMES):
        x0 = 52 + index * (width + 8)
        c.box(x0, y0 + 28, x0 + width, y1 - 6, fill=BG, outline=mix(EVENT, BG, 0.4), radius=6)
        size = 11 if c.text_width(name, 11, True) <= width - 8 else 10
        c.text(x0 + width / 2, (y0 + 28 + y1 - 6) / 2, name, size, True, EVENT, "mm")
    dashed(c, [(420, services_bottom), (420, y0)], EVENT)
    dashed(c, [(700, y0), (700, services_bottom)], EVENT)

    top = y1 + 22
    namespace(
        c, 40, top, 720, top + 152, "windops-data", "reached only by the services that own it"
    )
    c.box(56, top + 30, 452, top + 140, fill=KINDS["service"][1], outline=GREEN, width=1.25)
    c.text(68, top + 46, "PostgreSQL", 14, True)
    lines = (
        "20 databases, one per service, each with its own login",
        "Forced row-level security on every tenant table",
        "Telemetry partitioned by day (ADR 0003)",
        "Locally one pod; on Azure, Flexible Server with Entra sign-in",
    )
    for index, value in enumerate(lines):
        c.dot(72, top + 68 + index * 18, 2.5, GREEN)
        c.text(82, top + 68 + index * 18, value, 12, color=MUTED)
    c.node(464, top + 30, 584, top + 140, "Redis", "cache and rate limits", "service", 14, 11.5)
    c.node(596, top + 30, 706, top + 140, "Qdrant", "vector search", "service", 14, 11.5)
    c.arrow([(240, services_bottom + 76), (240, top)], width=1.25)

    namespace(c, 740, top, 1080, top + 152, "External", "through breakers")
    for index, (name, note) in enumerate(PROVIDERS):
        x0 = 754 + (index % 3) * 108
        y0 = top + 30 + (index // 3) * 58
        c.node(x0, y0, x0 + 100, y0 + 50, name, note, "external", 12.5, 10.5)
    c.arrow([(910, services_bottom + 76), (910, top)], width=1.25)

    top += 172
    observability = (
        ("Logs", "Loki locally, Log Analytics on Azure"),
        ("Metrics and alerts", "Prometheus, or managed Prometheus"),
        ("Traces", "Tempo, or Application Insights"),
        ("Dashboards", "Grafana, or Managed Grafana"),
    )
    namespace(c, 40, top, 1080, top + 92, "Observability", "from every workload, one trace ID")
    width = (1040 - 3 * 10 - 24) / 4
    for index, (name, note) in enumerate(observability):
        x0 = 52 + index * (width + 10)
        c.box(x0, top + 30, x0 + width, top + 80, fill=mix(BG, SIGNAL, 0.12), outline=SIGNAL)
        c.label(x0, top + 30, x0 + width, top + 80, name, note, TEXT, MUTED, 13, 11.5)

    draw_azure(c, top + 112)


def draw_azure(c: Canvas, top: float) -> None:
    """What the platform runs on in Azure, as docs/azure-plan.md sizes and prices it."""
    azure = KINDS["external"][0]
    c.box(40, top, 1080, top + AZURE_PANEL_HEIGHT, fill=mix(BG, azure, 0.08), outline=azure,
          radius=10)  # fmt: skip
    c.text(54, top + 16, "On Azure (ADR 0006)", 13, True, azure)
    c.text(
        64 + c.text_width("On Azure (ADR 0006)", 13, True), top + 16,
        "Sweden Central. Created in M4A.6 once its plan and price are approved",
        12, color=MUTED,
    )  # fmt: skip
    width = (1040 - 3 * 10 - 24) / 4
    for index, (name, note) in enumerate(AZURE_RESOURCES):
        x0 = 52 + (index % 4) * (width + 10)
        y0 = top + 30 + (index // 4) * 52
        c.node(x0, y0, x0 + width, y0 + 44, name, note, "external", 12.5, 11)


def controls_height(c: Canvas, controls: tuple[Control, ...], width: float) -> float:
    rows = 0
    for control in controls:
        suffix = f"  ({control.module})" if control.module else ""
        rows += len(c.wrap(control.text + suffix, 13, width - 20))
    return rows * 17 + len(controls) * 3


def draw_side(c: Canvas, x0: float, x1: float, bottom: float) -> None:
    """Security at each hop, failure behavior, the agents' limits and where it runs."""
    inner = x1 - x0 - 32
    height = 44 + sum(26 + controls_height(c, group, inner) for _, group in HOP_CONTROLS)
    top = 112.0
    c.box(x0, top, x1, top + height, fill=PANEL, radius=10)
    c.text(x0 + 16, top + 18, "Security on each hop", 14, True)
    draw_control_marker(c, x1 - 200, top + 18, False)
    c.text(x1 - 190, top + 18, "in place", 12, color=MUTED)
    draw_control_marker(c, x1 - 118, top + 18, True)
    c.text(x1 - 108, top + 18, "planned (module)", 12, color=MUTED)
    y = top + 36
    for title, controls in HOP_CONTROLS:
        c.text(x0 + 16, y + 8, title, 12.5, True, KINDS["external"][0])
        y = draw_controls(c, x0 + 16, y + 18, controls, inner) + 8

    top += height + 16
    c.box(x0, top, x1, top + 146, fill=PANEL, radius=10)
    c.text(x0 + 16, top + 18, "When a dependency fails", 14, True)
    states = (("Closed", GREEN), ("Open", RED), ("Half-open", AMBER))
    x = x0 + 16
    for index, (name, color) in enumerate(states):
        width = c.text_width(name, 12, True) + 16
        c.box(x, top + 32, x + width, top + 52, fill=mix(BG, color, 0.18), outline=color, radius=5)
        c.text(x + width / 2, top + 42, name, 12, True, TEXT, "mm")
        x += width
        if index < 2:
            c.arrow([(x + 2, top + 42), (x + 20, top + 42)])
            x += 24
    c.text(x + 8, top + 42, "50% of 10 calls fail; retry after 30 s", 11.5, color=MUTED)
    y = top + 72
    for verb, color, names in BREAKER_OUTCOMES:
        c.text(x0 + 16, y, verb, 12.5, True, color)
        c.text(x0 + 92, y, names, 12, color=MUTED)
        y += 24

    top += 162
    c.box(x0, top, x1, top + 126, fill=PANEL, outline=mix(AMBER, BG, 0.4), radius=10)
    c.text(x0 + 16, top + 18, "Agent guardrails", 14, True, AMBER)
    y = top + 40
    for rule in GUARDRAILS:
        c.dot(x0 + 22, y, 2.5, AMBER)
        c.text(x0 + 32, y, rule, 12.5)
        y += 21

    top += 142
    c.box(x0, top, x1, bottom, fill=PANEL, radius=10)
    c.text(x0 + 16, top + 18, "Where it runs", 14, True)
    y = top + 42
    for name, note in RUNTIMES:
        c.text(x0 + 16, y, name, 12.5, True, KINDS["external"][0])
        for index, row in enumerate(c.wrap(note, 12.5, x1 - x0 - 120)):
            c.text(x0 + 104, y + index * 17, row, 12.5, color=MUTED)
        y += 17 * len(c.wrap(note, 12.5, x1 - x0 - 120)) + 8
    c.line([(x0 + 16, y), (x1 - 16, y)], LINE, 1)
    c.text(x0 + 16, y + 18, "Azure, per month", 13, True)
    c.text(x1 - 16, y + 18, "docs/azure-plan.md", 11.5, color=MUTED, anchor="rm")
    y += 40
    for name, note, price in AZURE_COSTS:
        c.text(x0 + 16, y, name, 12.5, True, KINDS["external"][0])
        c.text(x0 + 104, y, note, 12.5, color=MUTED)
        c.text(x1 - 16, y, price, 13, True, GREEN if name == "Demo" else TEXT, "rm")
        y += 21
    if y - 8 > bottom:
        print("WARNING: the side column overflows")


def draw_roadmap(c: Canvas, y: float, x0: float, x1: float) -> float:
    """Draw every delivery phase with its progress, from the tracker, and return the y below."""
    phases = read_phase_progress()
    finished = sum(done for _, _, done, _ in phases)
    total = sum(count for _, _, _, count in phases)
    c.text(x0, y + 8, f"DELIVERY: {finished} OF {total} MODULES DONE", 12.5, True, MUTED)
    y += 22
    per_row, gap, height = 8, 8.0, 50.0
    width = (x1 - x0 - gap * (per_row - 1)) / per_row
    for index, (number, name, done, count) in enumerate(phases):
        bx = x0 + (index % per_row) * (width + gap)
        by = y + (index // per_row) * (height + gap)
        if done == count:
            accent, fill, state = GREEN, "#0D3B2E", "done"
        elif done:
            accent, fill, state = AMBER, KINDS["human"][1], "in progress"
        else:
            accent, fill, state = LINE, PANEL, "planned"
        c.box(bx, by, bx + width, by + height, fill=fill, outline=accent)
        c.label(
            bx, by, bx + width, by + height,
            f"{number}. {name}", f"{done} of {count}, {state}", TEXT, MUTED, 12, 11,
        )  # fmt: skip
    rows = math.ceil(len(phases) / per_row)
    return y + rows * height + (rows - 1) * gap


def build_platform_architecture() -> None:
    c = Canvas(1600, 1700)
    c.text(40, 40, "WindOps AI: AI-Assisted Fault Investigation for Renewable Assets", 28, True)
    c.text(
        40, 74,
        "Platform architecture from SRS v1.1, locally and on Azure (ADR 0006). A green dot "
        "marks a service running today.",
        15, color=MUTED,
    )  # fmt: skip
    legend = (
        ("ai", "AI agent"),
        ("rule", "Deterministic decision"),
        ("human", "Human decision"),
        ("service", "Service"),
        ("external", "External"),
    )
    x = 1560 - sum(c.text_width(name, 12.5) + 34 for _, name in legend) + 10
    for kind, name in legend:
        accent, fill = KINDS[kind]
        c.box(x, 32, x + 16, 48, fill=fill, outline=accent, radius=4, width=1.25)
        c.text(x + 22, 40, name, 12.5, color=MUTED)
        x += c.text_width(name, 12.5) + 34
    c.arrow([(1110, 74), (1150, 74)])
    c.text(1156, 74, "call", 12.5, color=MUTED)
    dashed(c, [(1200, 74), (1240, 74)], EVENT)
    c.text(1246, 74, "event", 12.5, color=MUTED)
    c.arrow([(1300, 74), (1340, 74)], SIGNAL)
    c.text(1346, 74, "device data", 12.5, color=MUTED)
    c.dot(1446, 74, 3.5, GREEN)
    c.text(1456, 74, "running today", 12.5, color=MUTED)

    draw_edge(c)
    draw_agents(c)
    _, services_bottom = draw_services(c)
    draw_events_and_data(c, services_bottom)
    left_bottom = services_bottom + 76 + 22 + 172 + 92 + 20 + AZURE_PANEL_HEIGHT
    draw_side(c, 1104, 1560, left_bottom)
    y = draw_roadmap(c, left_bottom + 20, 40, 1560) + 30
    c.text(
        800, y,
        "Agents recommend. Deterministic services and authorized humans decide. "
        "The system never issues turbine commands.",
        16, True, AMBER, "mm",
    )  # fmt: skip
    cropped = c.image.crop((0, 0, 1600 * c.s, int(y + 26) * c.s))
    ASSETS.mkdir(parents=True, exist_ok=True)
    cropped.save(ASSETS / "platform-architecture.png", optimize=True)


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
    build_platform_architecture()
    build_animation()
