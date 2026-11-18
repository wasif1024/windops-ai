"""What an upload must be before anything is kept, and what it says about itself.

- **Controls:** Markdown or plain text, judged by its bytes as well as its name; at most 1 MiB;
  strict UTF-8 with no control characters. Anything else is refused, whatever it calls itself.
- **Credentials:** a document holding a private key, an API key, a token or a password is refused
  outright, so none is ever kept or sent to the embedding provider (SRS 9.6).
- **Metadata:** read from the header table under the document's title, as a controlled document
  states it (FR-KNW-002). The request may give only what the header does not; a field the two give
  differently is refused, as one of them is wrong.
- **Screening:** a passage addressing instructions to an AI is reported, without the matched text,
  and the service holds the document until a person releases it (LLM-008).
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

MAXIMUM_BYTES = 1024 * 1024
CONTENT_TYPES = {"text/markdown": (".md",), "text/plain": (".txt",)}
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
TYPES = ("manual", "bulletin", "procedure", "reference", "engineering")
FIELDS = ("document_id", "revision", "title", "type", "published", "issued_by", "applies_to",
          "classification", "supersedes")  # fmt: skip
# The header's labels, as a controlled document prints them.
LABELS = {
    "Document": "document_id", "Revision": "revision", "Type": "type", "Published": "published",
    "Issued by": "issued_by", "Applies to": "applies_to", "Classification": "classification",
    "Supersedes": "supersedes",
}  # fmt: skip
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DOCUMENT_ID = re.compile(r"[A-Z0-9][A-Z0-9-]{2,63}")
_APPLIES_TO = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")
_SUPERSEDES = re.compile(r"(?P<id>[A-Z0-9][A-Z0-9-]{2,63}) revision (?P<revision>\d{1,4})")
_ROW = re.compile(r"^\| *(?P<label>[A-Za-z][A-Za-z ]*?) *\| *(?P<value>.*?) *\|$")
# Tab, line feed and carriage return are text; every other control character is not.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAXIMUM_TEXT = 500

# Credentials, by their shape. Each is refused, wherever it appears.
CREDENTIALS = (
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("an API key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a JSON web token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")),
    ("a password", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S{4,}")),
)  # fmt: skip
# Text addressed to an AI, as an injected instruction is (LLM-008). A finding holds the document
# for a person; it does not decide anything about it.
INSTRUCTIONS_TO_AI = (
    ("It tells the reader to ignore earlier instructions",
     re.compile(r"(?i)\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b(?:previous|prior|earlier|above"
                r"|system)\b[^.\n]{0,20}\b(?:instructions?|prompts?|rules)\b")),
    ("It claims to override the system", re.compile(r"(?i)\bsystem\s+override\b")),
    ("It addresses AI assistants or models directly",
     re.compile(r"(?i)\b(?:note|message|instructions?)\s+(?:to|for)\s+(?:the\s+)?(?:ai|automated|"
                r"llm|language model|assistant)")),
    ("It tells the reader to hide something from the user",
     re.compile(r"(?i)\bdo not (?:mention|reveal|tell)\b[^.\n]{0,40}\b(?:this|the user|the "
                r"operator)\b")),
    ("It tells the reader what role to take", re.compile(r"(?i)\byou are now\b")),
)  # fmt: skip


class UploadRefusedError(ValueError):
    """The upload cannot be kept, and why, in words a person can act on."""


@dataclass(frozen=True)
class Metadata:
    document_id: str
    revision: int
    title: str
    type: str
    published: str
    issued_by: str
    applies_to: tuple[str, ...]
    classification: str
    supersedes_document_id: str = ""
    supersedes_revision: int = 0


@dataclass(frozen=True)
class Finding:
    kind: str
    description: str
    line: int


@dataclass(frozen=True)
class Upload:
    text: str
    content_hash: str
    size_bytes: int
    metadata: Metadata
    from_document: tuple[str, ...]
    screening: tuple[Finding, ...] = field(default_factory=tuple)


def decode(filename: str, content_type: str, content: bytes) -> str:
    """The upload's text, once its name, type, size and bytes are acceptable."""
    if not _FILENAME.fullmatch(filename) or ".." in filename:
        raise UploadRefusedError(
            "The filename may hold only letters, digits, dots, hyphens and underscores"
        )
    extensions = CONTENT_TYPES.get(content_type)
    if extensions is None:
        raise UploadRefusedError(
            "Only Markdown (text/markdown) or plain text (text/plain) is taken"
        )
    if not filename.lower().endswith(extensions):
        raise UploadRefusedError(f"A {content_type} document's filename ends {extensions[0]}")
    if not content:
        raise UploadRefusedError("The document is empty")
    if len(content) > MAXIMUM_BYTES:
        raise UploadRefusedError(f"A document is at most {MAXIMUM_BYTES // 1024} KiB")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UploadRefusedError("The document is not text: it is not valid UTF-8") from error
    text = text.removeprefix("﻿")
    if _CONTROL.search(text):
        raise UploadRefusedError("The document is not text: it holds control characters")
    if not text.strip():
        raise UploadRefusedError("The document is empty")
    return text


def credentials_in(text: str) -> list[tuple[str, int]]:
    """Each credential found, by what it is and its line. Never the credential itself."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in CREDENTIALS:
            if pattern.search(line):
                found.append((name, number))
    return found


def screen(text: str) -> tuple[Finding, ...]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for description, pattern in INSTRUCTIONS_TO_AI:
            if pattern.search(line):
                findings.append(Finding("instructions_to_ai", description, number))
    return tuple(findings)


def header_of(text: str) -> dict[str, str]:
    """The title, and the header table's fields, from the top of a document, before its body."""
    lines = text.splitlines()
    found: dict[str, str] = {}
    if lines and lines[0].startswith("# "):
        found["title"] = lines[0][2:].strip()
    for line in lines[1:]:
        if line.startswith(("#", ">")):
            break
        match = _ROW.match(line.strip())
        if match and match["label"] in LABELS:
            found[LABELS[match["label"]]] = match["value"]
    return found


def _given(requested: Metadata | None) -> dict[str, str]:
    """What the request states, as text, leaving out what it left empty."""
    if requested is None:
        return {}
    given = {
        "document_id": requested.document_id, "title": requested.title, "type": requested.type,
        "published": requested.published, "issued_by": requested.issued_by,
        "classification": requested.classification,
        "applies_to": ", ".join(requested.applies_to),
    }  # fmt: skip
    if requested.revision:
        given["revision"] = str(requested.revision)
    if requested.supersedes_document_id:
        given["supersedes"] = (
            f"{requested.supersedes_document_id} revision {requested.supersedes_revision}"
        )
    return {name: value.strip() for name, value in given.items() if value.strip()}


def _same(name: str, stated: str, given: str) -> bool:
    if name == "applies_to":
        return [a.strip() for a in stated.split(",")] == [a.strip() for a in given.split(",")]
    return stated == given


def _metadata(values: dict[str, str]) -> Metadata:
    missing = [name for name in FIELDS if name != "supersedes" and not values.get(name)]
    if missing:
        raise UploadRefusedError(
            "The document's header does not state, and the upload does not give: "
            + ", ".join(missing)
        )
    document_id = values["document_id"]
    if not _DOCUMENT_ID.fullmatch(document_id):
        raise UploadRefusedError("A document ID is 3 to 64 uppercase letters, digits and hyphens")
    try:
        revision = int(values["revision"])
    except ValueError as error:
        raise UploadRefusedError("The revision is a whole number") from error
    if not 1 <= revision <= 9999:
        raise UploadRefusedError("The revision is from 1 to 9999")
    if values["type"] not in TYPES:
        raise UploadRefusedError(f"The type is one of {', '.join(TYPES)}")
    if values["classification"] not in CLASSIFICATIONS:
        raise UploadRefusedError(f"The classification is one of {', '.join(CLASSIFICATIONS)}")
    try:
        date.fromisoformat(values["published"])
    except ValueError as error:
        raise UploadRefusedError("The publication date is YYYY-MM-DD") from error
    if len(values["published"]) != 10:
        raise UploadRefusedError("The publication date is YYYY-MM-DD")
    applies_to = tuple(a.strip() for a in values["applies_to"].split(",") if a.strip())
    if not applies_to or not all(_APPLIES_TO.fullmatch(a) for a in applies_to):
        raise UploadRefusedError("It applies to turbine models or wind farm IDs, by name")
    for name in ("title", "issued_by"):
        if len(values[name]) > MAXIMUM_TEXT:
            raise UploadRefusedError(f"The {name.replace('_', ' ')} is too long")
    supersedes_id, supersedes_revision = "", 0
    if values.get("supersedes"):
        match = _SUPERSEDES.fullmatch(values["supersedes"])
        if match is None:
            raise UploadRefusedError('"Supersedes" names a document and its revision')
        supersedes_id, supersedes_revision = match["id"], int(match["revision"])
        if supersedes_id == document_id and supersedes_revision >= revision:
            raise UploadRefusedError("A revision supersedes only an earlier one")
    return Metadata(
        document_id=document_id, revision=revision, title=values["title"], type=values["type"],
        published=values["published"], issued_by=values["issued_by"], applies_to=applies_to,
        classification=values["classification"], supersedes_document_id=supersedes_id,
        supersedes_revision=supersedes_revision,
    )  # fmt: skip


def prepare(filename: str, content_type: str, content: bytes,
            requested: Metadata | None = None) -> Upload:  # fmt: skip
    """Everything checked, and the document's metadata, or UploadRefusedError saying why."""
    text = decode(filename, content_type, content)
    found = credentials_in(text)
    if found:
        where = ", ".join(f"{name} on line {line}" for name, line in found[:5])
        raise UploadRefusedError(
            f"The document holds credentials, so it is not taken: {where}. Remove them, and "
            "upload it again"
        )
    stated = header_of(text) if content_type == "text/markdown" else {}
    given = _given(requested)
    conflicts = [name for name in given if name in stated and not _same(name, stated[name],
                                                                        given[name])]  # fmt: skip
    if conflicts:
        raise UploadRefusedError(
            "The upload's metadata differs from the document's own header: "
            + ", ".join(conflicts)
            + ". The header is what the document says: leave those out"
        )
    metadata = _metadata({**given, **stated})
    return Upload(
        text=text, content_hash=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
        metadata=metadata, from_document=tuple(sorted(n for n in stated if n in FIELDS)),
        screening=screen(text),
    )  # fmt: skip
