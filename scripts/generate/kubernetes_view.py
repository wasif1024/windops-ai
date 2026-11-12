#!/usr/bin/env python3
"""Draw how WindOps AI runs on Azure Kubernetes Service, in the style of Azure's own diagrams.

    uv run python -m scripts.generate.kubernetes_view

It writes docs/assets/kubernetes-on-azure.png, the README's first picture of the platform on Azure:
a request from the browser to the pods, the pods in their namespaces, the managed data stores they
use, and how images and settings reach the cluster. The icons are drawn here, so no artwork is
copied. scripts/generate/readme_visuals.py runs this too.
"""

import math

from scripts.generate.readme_visuals import APPLICATION_GROUPS, ASSETS, Canvas, dashed

WHITE = "#FFFFFF"
INK = "#1F2937"
SOFT = "#4B5563"
RULE = "#CBD5E1"
NAMESPACE = "#F8FAFC"
BLUE = "#2563EB"
SKY = ("#7DD3FC", "#38BDF8", "#0284C7")  # a pod's top, left and right faces
VIOLET = ("#C4B5FD", "#A78BFA", "#6D28D9")  # an AI agent's pod
TEAL = ("#99F6E4", "#2DD4BF", "#0F766E")  # a deterministic decision's pod
AMBER = ("#FDE68A", "#FBBF24", "#B45309")  # a human decision's pod
GREY = ("#E2E8F0", "#94A3B8", "#475569")  # a pod that supports the others
GREEN = "#16A34A"

# Every domain service from the platform diagram, so the two never disagree.
BACK_END = tuple(name for _, _, members in APPLICATION_GROUPS for name, _ in members)
KIND_COLORS = {"service": SKY, "rule": TEAL, "human": AMBER}
BACK_END_KINDS = {name: kind for _, _, members in APPLICATION_GROUPS for name, kind in members}
FRONT_END = (
    ("React frontend", SKY),
    ("Web BFF", SKY),
    ("Identity", SKY),
    ("Authorization", SKY),
    ("Tenant", SKY),
    ("Keycloak", GREY),
)
MCP = ("WindOps MCP", "Documentation MCP", "Tavily MCP", "Weather MCP")
UTILITY = (
    ("Redis", "cache", GREY),
    ("Qdrant", "vector search", GREY),
    ("Collector", "OpenTelemetry", GREY),
    ("Prometheus agent", "managed", GREY),
    ("Key Vault CSI", "secrets", GREY),
    ("cert-manager", "mutual TLS", GREY),
)


# ------------------------------------------------------------------------------------------ icons


def cube(c: Canvas, x: float, y: float, size: float, faces: tuple[str, str, str]) -> None:
    """An isometric cube, as Azure draws a pod or container."""
    half, quarter = size / 2, size / 4
    top = [(x, y - half), (x + half, y - quarter), (x, y), (x - half, y - quarter)]
    left = [(x - half, y - quarter), (x, y), (x, y + half), (x - half, y + quarter)]
    right = [(x, y), (x + half, y - quarter), (x + half, y + quarter), (x, y + half)]
    for points, color in zip((top, left, right), faces, strict=True):
        c.draw.polygon([(px * c.s, py * c.s) for px, py in points], fill=color)


def pod(
    c: Canvas, x: float, y: float, name: str, faces: tuple[str, str, str] = SKY, note: str = ""
) -> None:
    """A pod: its cube in a ring, and its name below."""
    s = c.s
    c.draw.ellipse(
        ((x - 24) * s, (y - 24) * s, (x + 24) * s, (y + 24) * s),
        fill=WHITE,
        outline=RULE,
        width=max(1, round(1.25 * s)),
    )
    cube(c, x, y, 28, faces)
    rows = c.wrap(name, 11.5, 104, bold=True)
    for index, row in enumerate(rows):
        c.text(x, y + 36 + index * 14, row, 11.5, True, INK, "mm")
    if note:
        c.text(x, y + 36 + len(rows) * 14, note, 10.5, color=SOFT, anchor="mm")


def cylinder(c: Canvas, x: float, y: float, width: float, height: float, color: str) -> None:
    """A database."""
    s, rim = c.s, height * 0.18
    c.draw.rectangle(
        (
            (x - width / 2) * s,
            (y - height / 2 + rim / 2) * s,
            (x + width / 2) * s,
            (y + height / 2 - rim / 2) * s,
        ),
        fill=color,
    )
    c.draw.ellipse(
        (
            (x - width / 2) * s,
            (y + height / 2 - rim) * s,
            (x + width / 2) * s,
            (y + height / 2) * s,
        ),
        fill=color,
    )
    c.draw.ellipse(
        (
            (x - width / 2) * s,
            (y - height / 2) * s,
            (x + width / 2) * s,
            (y - height / 2 + rim) * s,
        ),
        fill="#93C5FD",
    )
    c.text(x, y + rim / 2, "SQL", 14, True, WHITE, "mm")


def queue(c: Canvas, x: float, y: float) -> None:
    """A message broker: messages in a queue."""
    c.box(x - 34, y - 22, x + 34, y + 22, fill="#1D4ED8", outline="#1D4ED8", radius=6)
    for index in range(3):
        left = x - 24 + index * 17
        c.box(left, y - 10, left + 13, y + 10, fill=WHITE, outline=WHITE, radius=2)
    c.head((x + 44, y), (x + 34, y), "#1D4ED8", 9)


def browser(c: Canvas, x: float, y: float) -> None:
    c.box(x - 40, y - 28, x + 40, y + 28, fill="#7DD3FC", outline="#0284C7", radius=5, width=1.5)
    c.box(x - 40, y - 28, x + 40, y - 16, fill="#0284C7", outline="#0284C7", radius=5)
    c.box(x - 30, y - 8, x + 30, y - 2, fill=WHITE, outline=WHITE, radius=2)
    c.box(x - 30, y + 6, x + 10, y + 12, fill=WHITE, outline=WHITE, radius=2)


def gateway(c: Canvas, x: float, y: float) -> None:
    """A load balancer or gateway: traffic spread from one point."""
    r = 30
    c.draw.polygon(
        [
            ((x) * c.s, (y - r) * c.s),
            ((x + r) * c.s, y * c.s),
            (x * c.s, (y + r) * c.s),
            ((x - r) * c.s, y * c.s),
        ],
        fill="#16A34A",
    )
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        c.arrow([(x + dx * 6, y + dy * 6), (x + dx * 19, y + dy * 19)], WHITE, 2)
    c.dot(x, y, 6, "#BBF7D0")


def ingress(c: Canvas, x: float, y: float) -> None:
    """The ingress: APISIX, every request's way in."""
    s = c.s
    c.draw.ellipse(
        ((x - 30) * s, (y - 30) * s, (x + 30) * s, (y + 30) * s),
        fill=WHITE,
        outline="#64748B",
        width=max(1, round(1.5 * s)),
    )
    c.draw.regular_polygon(((x * s, y * s), 20 * s), 6, rotation=30, fill="#DC2626")
    c.text(x, y + 1, "A", 18, True, WHITE, "mm")


def wheel(c: Canvas, x: float, y: float, radius: float = 26) -> None:
    """Kubernetes: a ship's wheel in a heptagon."""
    s = c.s
    c.draw.regular_polygon(((x * s, y * s), radius * s), 7, rotation=0, fill="#326CE5")
    c.draw.ellipse(
        (
            (x - radius * 0.5) * s,
            (y - radius * 0.5) * s,
            (x + radius * 0.5) * s,
            (y + radius * 0.5) * s,
        ),
        outline=WHITE,
        width=max(1, round(2.5 * s)),
    )
    for index in range(7):
        angle = -math.pi / 2 + index * 2 * math.pi / 7
        c.line(
            [(x, y), (x + radius * 0.72 * math.cos(angle), y + radius * 0.72 * math.sin(angle))],
            WHITE,
            2.5,
        )
    c.dot(x, y, radius * 0.16, WHITE)


def registry(c: Canvas, x: float, y: float) -> None:
    """A container registry: a cloud holding stacked containers."""
    for dx, dy, r in ((-20, 4, 18), (2, -8, 22), (22, 6, 16)):
        c.dot(x + dx, y + dy, r, "#60A5FA")
    c.box(x - 30, y + 4, x + 34, y + 22, fill="#60A5FA", outline="#60A5FA", radius=8)
    for row in range(3):
        c.box(
            x + 2, y - 10 + row * 12, x + 34, y + row * 12, fill="#64748B", outline=WHITE, radius=1
        )


def pipeline(c: Canvas, x: float, y: float) -> None:
    """Azure Pipelines: a run going forward."""
    c.box(x - 26, y - 26, x + 26, y + 26, fill="#2563EB", outline="#2563EB", radius=8)
    c.draw.polygon(
        [
            ((x - 10) * c.s, (y - 14) * c.s),
            ((x + 14) * c.s, y * c.s),
            ((x - 10) * c.s, (y + 14) * c.s),
        ],
        fill=WHITE,
    )


def person(c: Canvas, x: float, y: float) -> None:
    c.dot(x, y - 16, 12, "#38BDF8")
    c.draw.pieslice(
        ((x - 26) * c.s, (y - 2) * c.s, (x + 26) * c.s, (y + 46) * c.s), 180, 360, fill="#0EA5E9"
    )


def entra(c: Canvas, x: float, y: float) -> None:
    """Microsoft Entra ID: a pyramid."""
    s = c.s
    c.draw.polygon(
        [(x * s, (y - 32) * s), ((x + 30) * s, (y + 18) * s), ((x - 30) * s, (y + 18) * s)],
        fill="#38BDF8",
    )
    c.draw.polygon(
        [(x * s, (y - 32) * s), ((x + 30) * s, (y + 18) * s), (x * s, (y + 28) * s)], fill="#0284C7"
    )


def gauge(c: Canvas, x: float, y: float) -> None:
    """Azure Monitor: a dial."""
    s = c.s
    c.draw.ellipse(
        ((x - 30) * s, (y - 30) * s, (x + 30) * s, (y + 30) * s),
        fill="#DBEAFE",
        outline="#2563EB",
        width=max(1, round(4 * s)),
    )
    c.draw.arc(
        ((x - 20) * s, (y - 20) * s, (x + 20) * s, (y + 20) * s),
        150,
        390,
        fill="#7DD3FC",
        width=max(1, round(6 * s)),
    )
    c.line([(x, y), (x + 16, y - 10)], "#DC2626", 3)
    c.dot(x, y, 5, "#334155")


def key(c: Canvas, x: float, y: float) -> None:
    """Key Vault: a key."""
    c.dot(x, y, 30, "#FEF3C7")
    c.dot(x, y - 8, 12, "#F59E0B")
    c.dot(x, y - 8, 4.5, "#FEF3C7")
    c.box(x - 4, y, x + 4, y + 24, fill="#F59E0B", outline="#F59E0B", radius=2)
    c.box(x + 4, y + 12, x + 11, y + 16, fill="#F59E0B", outline="#F59E0B", radius=1)
    c.box(x + 4, y + 19, x + 9, y + 23, fill="#F59E0B", outline="#F59E0B", radius=1)


# ---------------------------------------------------------------------------------------- drawing


def caption(c: Canvas, x: float, y: float, title: str, sub: str = "", width: float = 230) -> None:
    c.text(x, y, title, 14, True, INK, "mm")
    if sub:
        for index, row in enumerate(c.wrap(sub, 11.5, width)):
            c.text(x, y + 17 + index * 14, row, 11.5, color=SOFT, anchor="mm")


def arrow_label(c: Canvas, x: float, y: float, value: str) -> None:
    width = c.text_width(value, 11.5)
    c.box(x - width / 2 - 4, y - 9, x + width / 2 + 4, y + 9, fill=WHITE, outline=WHITE, radius=3)
    c.text(x, y, value, 11.5, color=SOFT, anchor="mm")


def namespace_box(
    c: Canvas, x0: float, y0: float, x1: float, y1: float, title: str, name: str
) -> None:
    c.box(x0, y0, x1, y1, fill=NAMESPACE, outline=RULE, radius=4, width=1.25)
    c.text((x0 + x1) / 2, y0 + 18, title, 15, True, INK, "mm")
    c.text(x0 + 10, y1 - 12, f"Namespace {name}", 11, color=SOFT)


def dashed_box(c: Canvas, x0: float, y0: float, x1: float, y1: float, color: str) -> None:
    for start, end in (
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ):
        length = math.dist(start, end)
        position = 0.0
        while position < length:
            stop = min(position + 7, length)
            fraction, until = position / length, stop / length
            c.line(
                [
                    (
                        start[0] + (end[0] - start[0]) * fraction,
                        start[1] + (end[1] - start[1]) * fraction,
                    ),
                    (
                        start[0] + (end[0] - start[0]) * until,
                        start[1] + (end[1] - start[1]) * until,
                    ),
                ],
                color,
                1.75,
            )
            position += 12


def build_kubernetes_view() -> None:
    c = Canvas(1600, 1150, background=WHITE)
    c.text(40, 40, "WindOps AI on Azure Kubernetes Service", 26, True, INK)
    c.text(
        40,
        72,
        "How a request reaches the pods, and what each pod uses. The target architecture "
        "(ADR 0006); the detailed diagram below marks what runs today.",
        14,
        color=SOFT,
    )

    # The cluster, inside its virtual network.
    dashed_box(c, 404, 118, 1296, 892, BLUE)
    wheel(c, 446, 150)
    c.text(
        482, 150, "AKS cluster: Azure CNI Overlay powered by Cilium, Workload ID", 13, True, BLUE
    )
    c.text(1284, 150, "Virtual network 10.40.0.0/16", 12, True, BLUE, "rm")

    # Front end and identity.
    namespace_box(c, 420, 176, 640, 620, "Front end", "windops-system")
    ingress(c, 530, 240)
    c.text(530, 284, "APISIX gateway", 11.5, True, INK, "mm")
    c.text(530, 298, "the ingress", 10.5, color=SOFT, anchor="mm")
    c.arrow([(530, 308), (530, 330)], INK, 1.75)
    for index, (name, faces) in enumerate(FRONT_END):
        pod(c, 476 + (index % 2) * 108, 366 + (index // 2) * 84, name, faces)

    # Back-end services: every domain service, one pod each, scaled on its own.
    namespace_box(c, 660, 176, 1280, 620, "Back-end services", "windops-application")
    columns = 5
    for index, name in enumerate(BACK_END):
        x = 716 + (index % columns) * 118
        y = 236 + (index // columns) * 88
        pod(c, x, y, name, KIND_COLORS[BACK_END_KINDS[name]])
    # The Agent Runtime and a second replica, which the autoscaler adds under load.
    last = len(BACK_END)
    agent_y = 236 + (last // columns) * 88
    first_x = 716 + (last % columns) * 118
    second_x = 716 + (columns - 1) * 118
    pod(c, first_x, agent_y, "Agent Runtime", VIOLET, "LangGraph, Claude")
    pod(c, second_x, agent_y, "Agent Runtime", VIOLET, "another replica")
    dashed(c, [(first_x + 30, agent_y), (second_x - 30, agent_y)], SOFT)
    arrow_label(c, (first_x + second_x) / 2, agent_y - 14, "pod autoscaling")
    c.arrow([(641, 400), (659, 400)], INK, 1.75)
    c.text(650, 384, "gRPC", 11, color=SOFT, anchor="mm")

    # Agents' tools, and the pods that support everything.
    namespace_box(c, 420, 640, 640, 876, "Agent tools (MCP)", "windops-mcp")
    for index, name in enumerate(MCP):
        pod(c, 476 + (index % 2) * 108, 700 + (index // 2) * 84, name, GREY)
    namespace_box(c, 660, 640, 1280, 876, "Utility services", "windops-data, windops-observability")
    utility_x = [716 + index * 101 for index in range(len(UTILITY))]
    for x, (name, note, faces) in zip(utility_x, UTILITY, strict=True):
        pod(c, x, 720, name, faces, note)

    # The way in: the browser, App Gateway's WAF, then an internal load balancer to APISIX.
    browser(c, 110, 240)
    caption(c, 110, 290, "Client apps", "browsers and devices, HTTPS", 140)
    gateway(c, 290, 240)
    caption(c, 290, 290, "Application Gateway", "WAF v2, then an internal load balancer", 150)
    c.arrow([(152, 240), (258, 240)], INK, 2)
    c.arrow([(322, 240), (498, 240)], INK, 2)

    # The managed data stores the back end uses.
    c.text(1440, 200, "Managed data stores", 15, True, INK, "mm")
    cylinder(c, 1440, 270, 70, 74, "#2563EB")
    caption(c, 1440, 330, "Azure Database for PostgreSQL", "a database and Entra login per service")
    queue(c, 1440, 470)
    caption(c, 1440, 515, "Azure Service Bus", "events from each service's outbox")
    c.arrow([(1281, 270), (1398, 270)], INK, 2)
    c.arrow([(1281, 470), (1392, 470)], INK, 2)

    # Delivery: images to the registry, then manifests to the cluster.
    bottom = 990
    c.box(60, bottom - 66, 250, bottom + 80, fill=WHITE, outline=INK, radius=2, width=1.5)
    c.text(155, bottom - 46, "CI/CD", 15, True, INK, "mm")
    pipeline(c, 155, bottom - 4)
    caption(c, 155, bottom + 40, "Azure Pipelines", "after an approval")
    registry(c, 410, bottom)
    caption(c, 410, bottom + 46, "Azure Container Registry", "")
    dashed(c, [(256, bottom), (366, bottom)], INK)
    arrow_label(c, 311, bottom - 18, "docker push")
    dashed(c, [(500, 892), (452, bottom - 32)], INK)
    arrow_label(c, 520, 930, "image pull")
    dashed(c, [(155, bottom - 66), (155, 780), (402, 780)], INK)
    arrow_label(c, 270, 762, "kubectl apply, Kustomize")

    # Who may do what, and what the pods report and read.
    entra(c, 790, bottom)
    caption(c, 790, bottom + 46, "Microsoft Entra ID", "cluster access and Workload ID")
    c.line([(790, 893), (790, bottom - 40)], INK, 2)
    c.head((790, bottom - 34), (790, bottom - 50), INK, 8)
    arrow_label(c, 790, 920, "role-based access control")
    person(c, 620, bottom)
    caption(c, 620, bottom + 62, "Dev/Ops", "")
    c.arrow([(652, bottom + 10), (754, bottom + 10)], INK, 2)
    monitor_x, vault_x = 1010, 1210
    gauge(c, monitor_x, bottom)
    caption(c, monitor_x, bottom + 46, "Azure Monitor", "Application Insights, Prometheus, Grafana")
    for x in utility_x[2:4]:  # the Collector and the Prometheus agent
        c.arrow([(x, 792), (monitor_x + (x - utility_x[2]) * 0.3 - 6, bottom - 36)], INK, 1.75)
    key(c, vault_x, bottom)
    caption(c, vault_x, bottom + 46, "Azure Key Vault", "secrets, with no password stored")
    c.arrow([(utility_x[4], 792), (vault_x, bottom - 36)], INK, 1.75)
    c.text(
        800,
        1124,
        "Agents recommend. Deterministic services and authorized humans decide. "
        "The system never issues turbine commands.",
        14,
        True,
        "#B45309",
        "mm",
    )
    ASSETS.mkdir(parents=True, exist_ok=True)
    c.image.save(ASSETS / "kubernetes-on-azure.png", optimize=True)


if __name__ == "__main__":
    build_kubernetes_view()
