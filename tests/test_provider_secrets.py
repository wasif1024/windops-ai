"""Provider keys become Secrets only once set, and only where they are used (M7.2)."""

from scripts.setup.local_secrets import PROVIDER_KEYS, provider_secrets


def test_an_unset_key_makes_no_secret() -> None:
    assert provider_secrets({}) == []
    assert provider_secrets({"OPENAI_API_KEY": "  ", "TAVILY_API_KEY": ""}) == []


def test_a_set_key_goes_only_to_the_namespaces_that_use_it() -> None:
    made = provider_secrets({"OPENAI_API_KEY": "sk-test-0000"})
    assert [(m["metadata"], m["stringData"]) for m in made] == [
        ({"name": "windops-openai-api-key", "namespace": "windops-application"},
         {"api-key": "sk-test-0000"}),
    ]  # fmt: skip
    # No provider key is ever a setting, so none reaches the shared ConfigMap.
    assert all(not setting.startswith("WINDOPS_") for setting, _, _ in PROVIDER_KEYS)
