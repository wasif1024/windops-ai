"""A document split into passages, each small enough to cite and to embed (FR-KNW-003).

A passage stays inside one section, under its headings, so a citation says where it is, and never
splits a paragraph or a table unless that alone is too long. Each keeps its offsets and lines in
the document, so the cited text can be shown in place, and its own hash.
"""

import hashlib
import re
from dataclasses import dataclass

VERSION = "passages-1"
# About 400 tokens: far under the embedding model's limit, and short enough to cite.
TARGET_CHARACTERS = 1600
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Passage:
    ordinal: int
    section: str
    text: str
    start_offset: int
    end_offset: int
    first_line: int
    last_line: int

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


@dataclass
class _Block:
    """A paragraph, table or list: lines with no blank line between them."""

    start: int
    end: int
    first_line: int
    last_line: int


def _blocks(text: str) -> list[tuple[list[str], _Block]]:
    """Each block, with the headings it sits under."""
    blocks: list[tuple[list[str], _Block]] = []
    headings: list[tuple[int, str]] = []
    offset, current = 0, None
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        bare = line.rstrip("\r\n")
        heading = _HEADING.match(bare)
        if heading:
            level = len(heading[1])
            headings = [h for h in headings if h[0] < level] + [(level, heading[2])]
        if heading or not bare.strip():
            current = None
        if bare.strip() and not (heading and level > 1):
            # A title starts the first block; any other heading only names what follows it.
            if current is None:
                current = _Block(offset, offset + len(bare), number, number)
                blocks.append(([h[1] for h in headings], current))
            else:
                current.end, current.last_line = offset + len(bare), number
        offset += len(line)
    return blocks


def split(text: str) -> list[Passage]:
    passages: list[Passage] = []
    group: list[_Block] = []
    section: list[str] = []

    def flush() -> None:
        if not group:
            return
        start, end = group[0].start, group[-1].end
        passages.append(Passage(
            ordinal=len(passages), section=" > ".join(section), text=text[start:end],
            start_offset=start, end_offset=end, first_line=group[0].first_line,
            last_line=group[-1].last_line,
        ))  # fmt: skip
        group.clear()

    for headings, block in _blocks(text):
        if headings != section:
            flush()
            section = headings
        if group and block.end - group[0].start > TARGET_CHARACTERS:
            flush()
        group.append(block)
    flush()
    return passages
