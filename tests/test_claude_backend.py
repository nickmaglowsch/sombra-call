"""Backend selection for the Claude ``network`` tests (#89), without the network."""

import pytest

from claude_backend import (
    CACHE_NOT_COMPARABLE,
    COST_NOT_OBSERVABLE,
    NO_CLI,
    NO_KEY,
    api_key,
    claude_backend,
    env_model,
)


def _on_path(name: str) -> str | None:
    return f"/usr/local/bin/{name}"


def _missing(name: str) -> str | None:
    return None


def test_api_key_picks_the_api() -> None:
    assert claude_backend({"ANTHROPIC_API_KEY": "sk-test"}, _missing) == "api"


def test_api_key_wins_over_the_login() -> None:
    env = {"ANTHROPIC_API_KEY": "sk-test", "SOMBRA_CLAUDE_LOGIN": "1"}
    assert claude_backend(env, _on_path) == "api"


def test_login_without_key_picks_the_cli() -> None:
    assert claude_backend({"SOMBRA_CLAUDE_LOGIN": "1"}, _on_path) == "cli"


def test_empty_key_counts_as_unset() -> None:
    env = {"ANTHROPIC_API_KEY": "", "SOMBRA_CLAUDE_LOGIN": "1"}
    assert claude_backend(env, _on_path) == "cli"


@pytest.mark.parametrize("env", [{}, {"SOMBRA_CLAUDE_LOGIN": "0"}, {"SOMBRA_CLAUDE_LOGIN": "yes"}])
def test_without_the_opt_in_it_skips_as_before(env: dict[str, str]) -> None:
    with pytest.raises(pytest.skip.Exception, match="ANTHROPIC_API_KEY not set") as info:
        claude_backend(env, _on_path)
    assert str(info.value) == NO_KEY


def test_login_without_the_cli_skips() -> None:
    with pytest.raises(pytest.skip.Exception) as info:
        claude_backend({"SOMBRA_CLAUDE_LOGIN": "1"}, _missing)
    assert str(info.value) == NO_CLI


def test_reads_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert claude_backend() == "api"
    assert api_key() == "sk-test"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("SOMBRA_CLAUDE_LOGIN", raising=False)
    with pytest.raises(pytest.skip.Exception):
        claude_backend()


def test_env_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOMBRA_CLAUDE_CODE_MODEL", raising=False)
    assert env_model() is None
    monkeypatch.setenv("SOMBRA_CLAUDE_CODE_MODEL", "")
    assert env_model() is None
    monkeypatch.setenv("SOMBRA_SUMMARY_MODEL", "claude-haiku-4-5")
    assert env_model("SOMBRA_SUMMARY_MODEL") == "claude-haiku-4-5"


def test_skip_reasons_say_what_was_not_measured_and_how_to_get_it() -> None:
    assert "cache" in CACHE_NOT_COMPARABLE and "ANTHROPIC_API_KEY" in CACHE_NOT_COMPARABLE
    assert "cost" in COST_NOT_OBSERVABLE and "ANTHROPIC_API_KEY" in COST_NOT_OBSERVABLE
