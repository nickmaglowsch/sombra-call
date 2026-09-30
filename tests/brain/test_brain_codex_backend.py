"""brain.backend: ``brain.backend = "claude" | "codex"`` picks the Brain (PRD C5)."""

from __future__ import annotations

import pytest

from sombra.brain.backend import BACKENDS, DEFAULT_BACKEND, create_brain
from sombra.brain.claude import ClaudeBrain
from sombra.brain.codex import CodexBrain
from sombra.config.schema import BACKEND_ALIASES, BRAIN_BACKENDS, BrainConfig
from sombra.contracts import AutonomyLevel


def test_config_and_brain_agree_on_backends() -> None:
    # create_brain builds the two in-package backends; the config names claude "claude-api"
    # and adds "claude-code", which the orchestrator builds (#47).
    assert set(BACKENDS) == {"claude", "codex"}
    assert set(BRAIN_BACKENDS) == {"claude-code", "claude-api", "codex"}
    assert BACKEND_ALIASES[DEFAULT_BACKEND] == BrainConfig().backend


def test_codex_backend() -> None:
    brain = create_brain(
        "codex",
        user_name="Nick",
        aliases=("Nico",),
        allowed_topics=("roadmap",),
        level=AutonomyLevel.L1,
        model="m-codex",
    )
    assert isinstance(brain, CodexBrain)
    assert brain.settings.model == "m-codex"
    assert brain.settings.level is AutonomyLevel.L1
    assert tuple(brain.settings.allowed_topics) == ("roadmap",)
    assert isinstance(create_brain("codex", user_name="Nick"), CodexBrain)


def test_claude_backend() -> None:
    brain = create_brain("claude", user_name="Nick", api_key=lambda: "sk")
    assert isinstance(brain, ClaudeBrain)
    default_model = brain.settings.model
    other = create_brain("claude", user_name="Nick", model="claude-x", api_key=lambda: "sk")
    assert isinstance(other, ClaudeBrain)
    assert other.settings.model == "claude-x" != default_model


def test_claude_needs_a_key_and_unknown_backends_fail() -> None:
    with pytest.raises(ValueError, match="API key"):
        create_brain("claude", user_name="Nick")
    with pytest.raises(ValueError, match="claude, codex"):
        create_brain("gpt", user_name="Nick")
