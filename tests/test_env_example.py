"""Check that provider keys cannot reach git or an image by accident."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"


SECRET_ENDINGS = ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN")


def _entries() -> list[tuple[str, str]]:
    return [
        tuple(line.partition("=")[::2])  # type: ignore[misc]
        for line in EXAMPLE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_example_gives_secrets_no_values() -> None:
    entries = _entries()
    assert entries
    for name, value in entries:
        assert name.isupper()
        if name.endswith(SECRET_ENDINGS):
            assert value == "", f"{name} must have no value in the example"


def test_settings_have_local_defaults_and_none_looks_secret() -> None:
    settings = {name: value for name, value in _entries() if name.startswith("WINDOPS_")}
    assert settings["WINDOPS_PUBLIC_URL"].startswith("https://")
    for name, value in settings.items():
        assert value, f"{name} needs a local default in the example"
        assert not name.endswith(SECRET_ENDINGS), f"{name} would go into a ConfigMap"


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
