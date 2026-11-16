"""The synthetic OEM document set: manuals, bulletins and procedures for testing (SIM-007).

Every document is invented, and says so on its first page (SIM-006): a fictional manufacturer,
WindOps Synthetic, for the fictional demo models WS-8.0-167 and WS-4.5-145, and two fictional
operators' site procedures. Their figures agree with the simulator, the Asset Service's demo fleet
and the Policy Service's default rules, and tests hold them together, so an agent that cites a
document cites what the platform's own data shows.

Each document states its own metadata in a header table under its title, as a real controlled
document does. `manifest.json` records what that metadata is, and who each document is for, so
ingestion (M7.2) can be checked against it. A manifest entry is what the set's author knows, not
what a reader can see:

- `tenants`: the tenants the document is loaded for. Site procedures are each one tenant's own.
- `covers`: the fault labels and alarm codes it is the procedure for, for retrieval tests.
- `purpose`: "demo" to load for the demo tenants; "evaluation" for test material that is never
  loaded by default, such as a document carrying a prompt injection.
"""

import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
TYPES = ("manual", "bulletin", "procedure", "reference", "engineering")
PURPOSES = ("demo", "evaluation")
BANNER = "> **Synthetic document.** Written for testing WindOps AI,"
_ROW = re.compile(r"^\| (?P<field>[A-Z][a-z ]+) \| (?P<value>.+?) \|$")


@dataclass(frozen=True)
class Supersedes:
    document_id: str
    revision: int


@dataclass(frozen=True)
class Document:
    path: str
    document_id: str
    revision: int
    title: str
    type: str
    published: str
    issued_by: str
    applies_to: tuple[str, ...]
    classification: str
    supersedes: Supersedes | None
    tenants: tuple[str, ...]
    covers: tuple[str, ...]
    purpose: str

    def text(self) -> str:
        return (resources.files(__package__) / self.path).read_text(encoding="utf-8")


def _manifest() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (resources.files(__package__) / "manifest.json").read_text(encoding="utf-8")
    )
    return loaded


def set_version() -> str:
    return str(_manifest()["set_version"])


def load() -> tuple[Document, ...]:
    """Every document in the set, as the manifest records it."""
    documents = []
    for entry in _manifest()["documents"]:
        supersedes = entry["supersedes"]
        documents.append(
            Document(
                path=entry["path"],
                document_id=entry["document_id"],
                revision=entry["revision"],
                title=entry["title"],
                type=entry["type"],
                published=entry["published"],
                issued_by=entry["issued_by"],
                applies_to=tuple(entry["applies_to"]),
                classification=entry["classification"],
                supersedes=Supersedes(**supersedes) if supersedes else None,
                tenants=tuple(entry["tenants"]),
                covers=tuple(entry["covers"]),
                purpose=entry["purpose"],
            )
        )
    return tuple(documents)


def current(documents: tuple[Document, ...]) -> tuple[Document, ...]:
    """Each document's newest revision only: the ones another supersedes are left out."""
    replaced = {(d.supersedes.document_id, d.supersedes.revision) for d in documents
                if d.supersedes}  # fmt: skip
    return tuple(d for d in documents if (d.document_id, d.revision) not in replaced)


def read_header(text: str) -> dict[str, str]:
    """The title, and the header table's fields, as a reader sees them on the first page."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# "):
        raise ValueError("A document starts with its title")
    header = {"Title": lines[0][2:].strip()}
    for line in lines[1:]:
        if line.startswith("> "):
            break
        match = _ROW.match(line)
        if match and match["field"] != "Field":
            header[match["field"]] = match["value"]
    return header
