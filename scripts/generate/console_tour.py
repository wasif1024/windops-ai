#!/usr/bin/env python3
"""Compose the README's console screenshots into one picture, docs/assets/console-tour.png.

Eight desktop screens in order, from a turbine running hot to the audit trail of a person's
decision, then four more of the console around them, each in a browser frame with its address, the
role it was captured as, and a caption, and one phone screen, in the README pictures' palette. The
screenshots come from `make readme-screens` (console_screens.py), which captures them from the
running platform. Run this alone to compose them again:

    uv run python -m scripts.generate.console_tour
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from scripts.generate.readme_visuals import BG, LINE, MUTED, PANEL, TEXT, Canvas

ASSETS = Path(__file__).resolve().parents[2] / "docs" / "assets"
SCREENS_DIR = ASSETS / "screens"
OUT = ASSETS / "console-tour.png"
ACCENT = "#2DD4BF"
HUMAN = "#FBBF24"


@dataclass(frozen=True)
class Screen:
    file: str
    path: str
    title: str
    caption: str
    role: str


# From a fault to an audited decision, in order.
JOURNEY = (
    Screen("turbine-detail", "screens/fleet?turbine=dogger-east-t01", "A turbine runs hot",
           "Live readings and charts, suspect values marked", "Planner"),
    Screen("alarm-console", "screens/alarms", "Alarm console",
           "Most severe first; severity from the controller or catalogue", "Planner"),
    Screen("investigation-live", "screens/investigations?case=…", "Agents investigate",
           "The agent team's progress, followed live", "Engineer"),
    Screen("investigation", "screens/investigations?case=…", "Findings to review",
           "Each finding cited, for a person to accept or challenge", "Engineer"),
    Screen("evidence", "screens/investigations?case=…&evidence", "Frozen evidence",
           "The snapshot the run rested on, its hashes checked again", "Engineer"),
    Screen("work-order", "screens/maintenance?work_order=…", "An agent's draft",
           "Validated against parts, turbine and policy; only a person submits", "Planner"),
    Screen("approval", "screens/approvals?approval=…", "A person decides",
           "The plan, its validation and history, to approve or reject", "Manager"),
    Screen("audit-explorer", "screens/audit", "The audit trail",
           "Every decision recorded, the hash chain checked intact", "Auditor"),
)  # fmt: skip
# The console around it.
AROUND = (
    Screen("fleet-overview", "screens/fleet", "Fleet overview",
           "Every wind farm and turbine, live power, wind and freshness", "Planner"),
    Screen("maintenance-planner", "screens/maintenance", "Maintenance planner",
           "Work orders by status; an agent's draft is marked", "Planner"),
    Screen("knowledge-library", "screens/knowledge", "Knowledge library",
           "Manuals and procedures, searched with citations", "Engineer"),
    Screen("administration", "screens/admin", "Administration",
           "Each role's permissions; approval changes need a second admin",
           "Administrator"),
)  # fmt: skip
SCREENS = JOURNEY + AROUND
PHONE = "phone-investigation"

MARGIN, GAP, CARD_W, COLUMNS = 64, 48, 560, 4
BAR, CAPTION = 38, 78
SHOT_H = CARD_W * 900 // 1440
CARD_H = BAR + SHOT_H
ROW_H = CARD_H + CAPTION + 28
HEADER, LABEL = 176, 44
PHONE_GAP = 64


def rounded(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *image.size), radius, fill=255)
    out = Image.new("RGBA", image.size)
    out.paste(image, (0, 0), mask)
    return out


def shadow(canvas: Canvas, box: tuple[int, int, int, int], radius: int) -> None:
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", canvas.image.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((x0 + 4, y0 + 10, x1 + 4, y1 + 10), radius,
                                            fill=(0, 0, 0, 150))  # fmt: skip
    layer = layer.filter(ImageFilter.GaussianBlur(14))
    base = canvas.image.convert("RGBA")
    base.alpha_composite(layer)
    canvas.image.paste(base.convert("RGB"))


def pill(canvas: Canvas, x: int, y: int, label: str, color: str = ACCENT) -> int:
    """A small rounded label centred on y; return its width."""
    width = int(canvas.text_width(label, 12, bold=True)) + 20
    fill = "#46350A" if color == HUMAN else "#0D3B3B"
    canvas.draw.rounded_rectangle((x, y - 11, x + width, y + 11), 11, fill=fill, outline=color)
    canvas.text(x + 10, y, label, 12, bold=True, color=color)
    return width


def browser(canvas: Canvas, x: int, y: int, screen: Screen, step: int | None) -> None:
    shadow(canvas, (x, y, x + CARD_W, y + CARD_H), 14)
    d = canvas.draw
    d.rounded_rectangle((x, y, x + CARD_W, y + CARD_H), 14, fill="#E2E8F0")
    d.rectangle((x, y + BAR - 2, x + CARD_W, y + BAR), fill="#CBD5E1")
    for i, color in enumerate(("#F87171", "#FBBF24", "#34D399")):
        d.ellipse((x + 16 + i * 20, y + 13, x + 28 + i * 20, y + 25), fill=color)
    d.rounded_rectangle((x + 84, y + 8, x + CARD_W - 16, y + 30), 11, fill="#FFFFFF")
    canvas.text(x + 98, y + 19, f"windops.example/{screen.path}", 12, color="#475569")
    shot = Image.open(SCREENS_DIR / f"{screen.file}.png").convert("RGB")
    shot = shot.resize((CARD_W, shot.height * CARD_W // shot.width), Image.Resampling.LANCZOS)
    shot = shot.crop((0, 0, CARD_W, SHOT_H))
    # Rounded at the bottom only: the bar covers the top.
    framed = rounded(shot, 14)
    canvas.image.paste(framed, (x, y + BAR), framed)
    canvas.image.paste(shot.crop((0, 0, CARD_W, 14)), (x, y + BAR))
    # The caption: the step, the title, and the role it was captured as.
    cy = y + CARD_H + 22
    tx = x
    if step is not None:
        d.ellipse((x, cy - 14, x + 28, cy + 14), fill=ACCENT)
        canvas.text(x + 14, cy, str(step), 15, bold=True, color=BG, anchor="mm")
        tx = x + 38
    canvas.text(tx, cy, screen.title, 19, bold=True)
    role = screen.role
    width = int(canvas.text_width(role, 12, bold=True)) + 20
    pill(canvas, x + CARD_W - width, cy, role, HUMAN if role == "Manager" else ACCENT)
    canvas.text(x, cy + 30, screen.caption, 15, color=MUTED)


def arrow(canvas: Canvas, x: int, y: int) -> None:
    """A chevron in the gap between two cards of the journey."""
    cx, cy = x + GAP // 2, y + CARD_H // 2
    canvas.draw.line((cx - 7, cy - 12, cx + 5, cy, cx - 7, cy + 12), fill=ACCENT, width=4)


def phone(canvas: Canvas, x: int, y: int, height: int) -> int:
    shot = Image.open(SCREENS_DIR / f"{PHONE}.png").convert("RGB")
    bezel, radius = 16, 44
    inner_h = height - 2 * bezel
    inner_w = shot.width * inner_h // shot.height
    shot = shot.resize((inner_w, inner_h), Image.Resampling.LANCZOS)
    width = inner_w + 2 * bezel
    shadow(canvas, (x, y, x + width, y + height), radius)
    d = canvas.draw
    d.rounded_rectangle((x, y, x + width, y + height), radius, fill="#0F172A", outline="#475569",
                        width=3)  # fmt: skip
    framed = rounded(shot, radius - bezel)
    canvas.image.paste(framed, (x + bezel, y + bezel), framed)
    cx = x + width // 2
    d.rounded_rectangle((cx - 44, y + bezel + 8, cx + 44, y + bezel + 26), 9, fill="#0F172A")
    return width


def section(canvas: Canvas, y: int, label: str) -> None:
    canvas.text(MARGIN, y + LABEL // 2 - 4, label.upper(), 14, bold=True, color=MUTED)


def build() -> None:
    first = Image.open(SCREENS_DIR / f"{PHONE}.png")
    grid_w = COLUMNS * CARD_W + (COLUMNS - 1) * GAP
    rows = [HEADER + LABEL, HEADER + LABEL + ROW_H, HEADER + 2 * LABEL + 2 * ROW_H]
    phone_h = ROW_H + CARD_H
    phone_w = (phone_h - 32) * first.width // first.height + 32
    width = MARGIN + grid_w + PHONE_GAP + phone_w + MARGIN
    height = rows[2] + ROW_H + 70
    c = Canvas(width, height, scale=1, background=BG)
    d = c.draw
    # The header.
    d.rectangle((0, 0, width, 6), fill=ACCENT)
    c.text(MARGIN, 66, "WindOps AI: the operations console", 40, bold=True)
    c.text(MARGIN, 116, "Captured from the running platform in a real browser, each screen as "
                        "the role that uses it. Synthetic demo fleet; nothing here is a mock-up.",
           18, color=MUTED)  # fmt: skip
    label = "Responsive: desktop and phone"
    pw = c.text_width(label, 15, bold=True) + 32
    d.rounded_rectangle((width - MARGIN - pw, 50, width - MARGIN, 84), 17, fill=PANEL,
                        outline=LINE)  # fmt: skip
    c.text(width - MARGIN - pw + 16, 67, label, 15, bold=True, color=TEXT)
    # The journey, two rows of four, then the console around it.
    section(c, HEADER, "From a fault to an audited decision")
    for index, screen in enumerate(JOURNEY):
        row, column = divmod(index, COLUMNS)
        x = MARGIN + column * (CARD_W + GAP)
        browser(c, x, rows[row], screen, index + 1)
        if column < COLUMNS - 1:
            arrow(c, x + CARD_W, rows[row])
    section(c, rows[2] - LABEL, "Around it")
    for column, screen in enumerate(AROUND):
        browser(c, MARGIN + column * (CARD_W + GAP), rows[2], screen, None)
    # The phone, the height of the journey's frames.
    px = MARGIN + grid_w + PHONE_GAP
    phone(c, px, rows[0], phone_h)
    cy = rows[0] + phone_h + 22
    c.text(px, cy, "On a phone", 19, bold=True)
    pill(c, px + int(c.text_width("On a phone", 19, bold=True)) + 12, cy, "Engineer")
    c.text(px, cy + 30, "Step 4's findings, at 412 px wide", 15, color=MUTED)
    # The footer.
    motto = "Agents recommend and draft. Deterministic services and authorized people decide."
    c.text(MARGIN, height - 30, motto, 16, bold=True, color=HUMAN)
    c.image.save(OUT, optimize=True)


if __name__ == "__main__":
    build()
