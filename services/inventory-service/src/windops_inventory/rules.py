"""What a part, a line and a store's name must look like, and how a model is matched. Pure."""

import re
from collections.abc import Iterable

from windops.inventory.v1 import inventory_pb2 as pb

PART_NUMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,63}")
WIND_FARM = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
WORK_ORDER = re.compile(r"wo_[a-z0-9]{20}")
UNITS = ("each", "set", "kit", "litre", "kilogram", "metre")
MAXIMUM_NAME, MAXIMUM_DESCRIPTION, MAXIMUM_MODELS, MAXIMUM_TEXT = 200, 2000, 50, 100
MAXIMUM_LINES, MAXIMUM_QUANTITY, MAXIMUM_ON_HAND = 50, 10_000, 1_000_000
MAXIMUM_REASON = 500


def key(text: str) -> str:
    """A manufacturer or model as matched: trimmed, single-spaced, lowercased."""
    return " ".join(text.split()).lower()


def part_problem(part: pb.Part) -> str:
    """Why a part cannot be kept, or "" if it can."""
    if not PART_NUMBER.fullmatch(part.part_number):
        return "part_number is 2 to 64 letters, digits and . _ / -, starting with one of the first"
    if not part.name.strip() or len(part.name) > MAXIMUM_NAME:
        return f"name is needed, of at most {MAXIMUM_NAME} characters"
    if len(part.description) > MAXIMUM_DESCRIPTION:
        return f"description is at most {MAXIMUM_DESCRIPTION} characters"
    if not part.component_kind.strip() or len(part.component_kind) > MAXIMUM_TEXT:
        return "component_kind is needed, such as gearbox"
    if part.unit not in UNITS:
        return f"unit is one of: {', '.join(UNITS)}"
    if part.fits_any_model and part.fits:
        return "A part that fits any model names no models"
    if not part.fits_any_model and not part.fits:
        return "Name the turbine models it fits, or say it fits any model"
    if len(part.fits) > MAXIMUM_MODELS:
        return f"At most {MAXIMUM_MODELS} models"
    for model in part.fits:
        if not key(model.manufacturer) or not key(model.model):
            return "Each model it fits names its manufacturer and model"
        if len(model.manufacturer) > MAXIMUM_TEXT or len(model.model) > MAXIMUM_TEXT:
            return f"A manufacturer or model is at most {MAXIMUM_TEXT} characters"
    return ""


def lines_problem(lines: Iterable[pb.PartLine]) -> str:
    """Why these lines cannot be checked or reserved, or "" if they can."""
    given = list(lines)
    if not given:
        return "Name at least one part"
    if len(given) > MAXIMUM_LINES:
        return f"At most {MAXIMUM_LINES} parts"
    seen: set[str] = set()
    for line in given:
        if not PART_NUMBER.fullmatch(line.part_number):
            return f"{line.part_number!r} is not a part number"
        if not 1 <= line.quantity <= MAXIMUM_QUANTITY:
            return f"Each quantity is from 1 to {MAXIMUM_QUANTITY}"
        if line.part_number in seen:
            return f"{line.part_number} is named twice; give it once, with the total"
        seen.add(line.part_number)
    return ""


def wind_farm_problem(wind_farm_id: str) -> str:
    if not wind_farm_id or len(wind_farm_id) > 64 or not WIND_FARM.fullmatch(wind_farm_id):
        return "wind_farm_id is a wind farm's ID, such as dogger-east"
    return ""
