"""The published OpenAPI document matches the code, so a change to the API shows in review."""

from scripts.generate.openapi import DOCUMENT, generated


def test_the_published_document_matches_the_code() -> None:
    assert DOCUMENT.read_text() == generated(), "Run: make openapi"


def test_every_path_is_versioned() -> None:
    import json

    paths = json.loads(DOCUMENT.read_text())["paths"]
    assert paths
    assert all(path.startswith("/api/v1/") for path in paths)
