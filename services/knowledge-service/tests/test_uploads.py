"""What an upload must be, what it says about itself, and the passages it is cut into.

The synthetic document set's manifest records each document's metadata (M7.1), so extraction is
checked against it for every document in the set.
"""

import itertools

import pytest
from windops_knowledge import passages, uploads
from windops_simulator import documents

# The evaluation documents the upload screening flags.
SCREENED = {"ADV-SB-INJ-001", "ADV-PRC-INJ-003"}

DOCUMENTS = documents.load()


def prepare(text: str, filename: str = "doc.md", content_type: str = "text/markdown",
            **given: object) -> uploads.Upload:  # fmt: skip
    requested = uploads.Metadata(**given) if given else None  # type: ignore[arg-type]
    return uploads.prepare(filename, content_type, text.encode(), requested)


def refused(text: str | bytes, filename: str = "doc.md", content_type: str = "text/markdown",
            requested: uploads.Metadata | None = None) -> str:  # fmt: skip
    content = text.encode() if isinstance(text, str) else text
    with pytest.raises(uploads.UploadRefusedError) as error:
        uploads.prepare(filename, content_type, content, requested)
    return str(error.value)


HEADER = """# Gearbox Torque Values

| Field | Value |
| --- | --- |
| Document | ACME-TQ-001 |
| Revision | 2 |
| Type | reference |
| Published | 2026-01-05 |
| Issued by | A test |
| Applies to | WS-8.0-167 |
| Classification | internal |

## 1. Values

Tighten the bolts in a cross pattern.
"""


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda d: d.path)
def test_every_documents_metadata_is_read_from_its_own_header(
    document: documents.Document,
) -> None:
    upload = prepare(document.text(), document.path.rsplit("/", 1)[-1])
    m = upload.metadata
    assert (m.document_id, m.revision, m.title, m.type, m.published, m.issued_by, m.applies_to,
            m.classification) == (document.document_id, document.revision, document.title,
                                  document.type, document.published, document.issued_by,
                                  document.applies_to, document.classification)  # fmt: skip
    supersedes = (document.supersedes.document_id, document.supersedes.revision) \
        if document.supersedes else ("", 0)  # fmt: skip
    assert (m.supersedes_document_id, m.supersedes_revision) == supersedes
    assert "document_id" in upload.from_document and "classification" in upload.from_document
    # Only the evaluation material is addressed to an AI.
    # Screening flags an injection that addresses an AI in the words it knows. One evaluation
    # document is quiet on purpose, so the model's own resistance is tested too (MCP-008).
    assert bool(upload.screening) == (document.document_id in SCREENED)


def test_the_injected_bulletin_is_screened_line_by_line_without_quoting_it() -> None:
    injected = next(d for d in DOCUMENTS if d.purpose == "evaluation")
    findings = prepare(injected.text(), "adv.md").screening
    assert {f.kind for f in findings} == {"instructions_to_ai"}
    assert len({f.description for f in findings}) >= 3
    lines = injected.text().splitlines()
    assert all(1 <= f.line <= len(lines) for f in findings)
    # A finding says what it is, never what the document said.
    for finding in findings:
        assert finding.description not in lines[finding.line - 1]
        assert "approve" not in finding.description.lower()


@pytest.mark.parametrize(("filename", "content_type", "content", "says"), [
    ("doc.pdf", "application/pdf", b"%PDF-1.7", "Only Markdown"),
    ("doc.txt", "text/markdown", b"# x", "ends .md"),
    ("../etc/passwd.md", "text/markdown", b"# x", "filename"),
    ("doc.md", "text/markdown", b"", "empty"),
    ("doc.md", "text/markdown", b"   \n", "empty"),
    ("doc.md", "text/markdown", b"\xff\xfe\x00binary", "not valid UTF-8"),
    ("doc.md", "text/markdown", b"# x\x00y", "control characters"),
    ("doc.md", "text/markdown", b"x" * (uploads.MAXIMUM_BYTES + 1), "at most"),
])  # fmt: skip
def test_an_upload_that_is_not_plain_text_of_a_sane_size_is_refused(
    filename: str, content_type: str, content: bytes, says: str
) -> None:
    assert says in refused(content, filename, content_type)


# Fake credentials, each put together here, so no credential-shaped text is in the repository for
# a secret scanner to find. None is real.
@pytest.mark.parametrize(("secret", "name"), [
    ("-----BEGIN RSA " + "PRIVATE KEY-----", "a private key"),
    ("aws: " + "AKIA" + "ABCDEFGHIJKLMNOP", "an AWS access key"),
    ("key " + "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz123456", "an API key"),
    ("token " + "ghp_" + "a" * 36, "a GitHub token"),
    ("password: hunter2!", "a password"),
    ("eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0.abc", "a JSON web token"),
])  # fmt: skip
def test_a_document_holding_a_credential_is_refused_without_repeating_it(
    secret: str, name: str
) -> None:
    said = refused(HEADER + f"\nThe service login is below.\n{secret}\n")
    assert name in said and "line 18" in said
    assert secret not in said


def test_a_plain_text_document_needs_its_metadata_given() -> None:
    text = "Torque values for the filter housing.\n"
    assert "does not state" in refused(text, "doc.txt", "text/plain")
    upload = prepare(text, "doc.txt", "text/plain", document_id="ACME-TQ-002", revision=1,
                     title="Torque", type="reference", published="2026-02-01",
                     issued_by="A test", applies_to=("WS-4.5-145",),
                     classification="public")  # fmt: skip
    assert upload.metadata.document_id == "ACME-TQ-002" and upload.from_document == ()


def test_the_upload_may_fill_a_gap_but_never_contradict_the_header() -> None:
    without = HEADER.replace("| Classification | internal |\n", "")
    assert "does not state" in refused(without) and "classification" in refused(without)
    filled = prepare(without, document_id="", revision=0, title="", type="", published="",
                     issued_by="", applies_to=(), classification="confidential")  # fmt: skip
    assert filled.metadata.classification == "confidential"
    assert "classification" not in filled.from_document
    clash = uploads.Metadata(document_id="", revision=0, title="", type="", published="",
                             issued_by="", applies_to=(), classification="public")  # fmt: skip
    assert "differs from the document's own header: classification" in refused(
        HEADER, requested=clash
    )


@pytest.mark.parametrize(("row", "says"), [
    ("| Revision | 2 |", "| Revision | two |"),
    ("| Type | reference |", "| Type | memo |"),
    ("| Published | 2026-01-05 |", "| Published | 5 January 2026 |"),
    ("| Classification | internal |", "| Classification | secret |"),
    ("| Document | ACME-TQ-001 |", "| Document | acme tq |"),
])  # fmt: skip
def test_metadata_that_cannot_be_right_is_refused(row: str, says: str) -> None:
    refused(HEADER.replace(row, says))


def test_a_revision_supersedes_only_an_earlier_one() -> None:
    later = HEADER.replace(
        "| Classification | internal |",
        "| Classification | internal |\n| Supersedes | ACME-TQ-001 revision 1 |",
    )
    assert prepare(later).metadata.supersedes_revision == 1
    assert "earlier" in refused(later.replace("revision 1 |", "revision 2 |"))


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda d: d.path)
def test_passages_are_the_documents_own_text_in_order_under_their_headings(
    document: documents.Document,
) -> None:
    text = document.text()
    split = passages.split(text)
    lines = text.splitlines()
    assert [p.ordinal for p in split] == list(range(len(split)))
    for passage in split:
        assert text[passage.start_offset : passage.end_offset] == passage.text
        assert lines[passage.first_line - 1] == passage.text.splitlines()[0]
        assert passage.section.startswith(document.title)
        assert len(passage.text) <= passages.TARGET_CHARACTERS * 1.5
    # Every line of body text is in a passage; only headings and blank lines are left out.
    covered = {n for p in split for n in range(p.first_line, p.last_line + 1)}
    for number, line in enumerate(lines, start=1):
        if line.strip() and not line.startswith("## ") and not line.startswith("### "):
            assert number in covered, line
    assert all(p.start_offset < q.start_offset for p, q in itertools.pairwise(split))


def test_a_section_too_long_for_one_passage_is_cut_between_paragraphs() -> None:
    paragraphs = [f"Paragraph {n}. " + "word " * 120 for n in range(8)]
    text = "# Long\n\n## Body\n\n" + "\n\n".join(paragraphs) + "\n"
    split = passages.split(text)
    body = [p for p in split if p.section == "Long > Body"]
    assert len(body) > 1
    assert all(p.text.startswith("Paragraph") for p in body)
    assert "".join(p.text for p in body).count("Paragraph") == len(paragraphs)
