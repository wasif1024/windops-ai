#!/usr/bin/env python3
"""Compose the README's console screenshots into one picture, docs/assets/console-tour.png.

Six desktop screens, each in a browser frame with its address and a caption, and one phone
screen, in the README pictures' palette. The screenshots come from `make readme-screens`
(console_screens.py), which captures them from the running platform. Run this alone to compose
them again:

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


@dataclass(frozen=True)
class Screen:
    file: str
    path: str
    title: str
    caption: str
    phase: str


SCREENS = (
    Screen("fleet-overview", "screens/fleet", "Fleet overview",
           "Every wind farm and turbine, live power, wind and freshness", "Phase 4"),
    Screen("turbine-detail", "screens/fleet?turbine=dogger-east-t01", "Turbine detail",
           "Latest readings and live charts, suspect values marked", "Phase 4"),
    Screen("alarm-console", "screens/alarms", "Alarm console",
           "Most severe first; severity from the controller or catalogue", "Phase 5"),
    Screen("maintenance-planner", "screens/maintenance", "Maintenance planner",
           "Work orders by status; an agent's draft is marked", "Phase 9"),
    Screen("work-order", "screens/maintenance?work_order=…", "An agent's draft",
           "Validated against parts, turbine and policy; only a person submits", "Phase 9"),
    Screen("parts-lookup", "screens/parts?turbine=dogger-east-t01", "Parts lookup",
           "Parts that fit the turbine, with its wind farm's stock", "Phase 9"),
)  # fmt: skip
PHONE = "phone-work-order"

MARGIN, GAP, CARD_W = 64, 40, 640
BAR, CAPTION = 38, 78
SHOT_H = CARD_W * 900 // 1440
CARD_H = BAR + SHOT_H
ROW_H = CARD_H + CAPTION + 28
HEADER = 176
PHONE_GAP = 56


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


def browser(canvas: Canvas, x: int, y: int, screen: Screen) -> None:
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
    # The caption.
    cy = y + CARD_H + 22
    width = canvas.text_width(screen.phase, 12, bold=True) + 20
    d.rounded_rectangle((x, cy - 11, x + width, cy + 11), 11, fill="#0D3B3B", outline=ACCENT)
    canvas.text(x + 10, cy, screen.phase, 12, bold=True, color=ACCENT)
    canvas.text(x + width + 12, cy, screen.title, 19, bold=True)
    canvas.text(x, cy + 30, screen.caption, 15, color=MUTED)


def phone(canvas: Canvas, x: int, y: int, height: int) -> None:
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


def build() -> None:
    first = Image.open(SCREENS_DIR / f"{PHONE}.png")
    grid_w = 3 * CARD_W + 2 * GAP
    grid_h = 2 * ROW_H
    phone_h = ROW_H + CARD_H
    phone_w = (phone_h - 32) * first.width // first.height + 32
    width = MARGIN + grid_w + PHONE_GAP + phone_w + MARGIN
    height = HEADER + grid_h + 70
    c = Canvas(width, height, scale=1, background=BG)
    d = c.draw
    # The header.
    d.rectangle((0, 0, width, 6), fill=ACCENT)
    c.text(MARGIN, 66, "WindOps AI: the operations console", 40, bold=True)
    c.text(MARGIN, 116, "Captured from the running platform in a real browser, signed in as a "
                        "Maintenance Planner. Synthetic demo fleet; nothing here is a mock-up.",
           18, color=MUTED)  # fmt: skip
    pill = "Responsive: desktop and phone"
    pw = c.text_width(pill, 15, bold=True) + 32
    d.rounded_rectangle((width - MARGIN - pw, 50, width - MARGIN, 84), 17, fill=PANEL,
                        outline=LINE)  # fmt: skip
    c.text(width - MARGIN - pw + 16, 67, pill, 15, bold=True, color=TEXT)
    # The six desktop screens.
    for index, screen in enumerate(SCREENS):
        row, column = divmod(index, 3)
        browser(c, MARGIN + column * (CARD_W + GAP), HEADER + row * ROW_H, screen)
    # The phone, the height of both rows' frames.
    px = MARGIN + grid_w + PHONE_GAP
    phone(c, px, HEADER, phone_h)
    cy = HEADER + phone_h + 22
    label = "On a phone"
    lw = c.text_width("Phase 9", 12, bold=True) + 20
    d.rounded_rectangle((px, cy - 11, px + lw, cy + 11), 11, fill="#0D3B3B", outline=ACCENT)
    c.text(px + 10, cy, "Phase 9", 12, bold=True, color=ACCENT)
    c.text(px + lw + 12, cy, label, 19, bold=True)
    c.text(px, cy + 30, "The same draft at 412 px wide", 15, color=MUTED)
    # The footer.
    motto = "Agents recommend and draft. Deterministic services and authorized people decide."
    c.text(MARGIN, height - 30, motto, 16, bold=True, color="#FBBF24")
    c.image.save(OUT, optimize=True)


if __name__ == "__main__":
    build()
