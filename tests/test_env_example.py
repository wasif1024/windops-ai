"""Check that provider keys cannot reach git or an image by accident."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"


def test_example_contains_names_but_no_values() -> None:
    entries = [
        line
        for line in EXAMPLE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries
    for entry in entries:
        name, _, value = entry.partition("=")
        assert name.isupper()
        assert value == "", f"{name} must have no value in the example"


def test_example_lists_every_provider_key_in_the_srs() -> None:
    text = EXAMPLE.read_text()
    for name in ("ANTHROPIC_API_KEY", "LANGSMITH_API_KEY", "TAVILY_API_KEY", "OPENAI_API_KEY"):
        assert f"{name}=" in text


def test_git_ignores_real_env_files_but_keeps_the_example() -> None:
    rules = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in rules
    assert ".env.*" in rules
    assert "!.env.example" in rules


def test_images_never_receive_env_files() -> None:
    rules = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in rules
    assert ".env.*" in rules
