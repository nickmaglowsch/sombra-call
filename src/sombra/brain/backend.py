"""Backend selection (PRD C5): ``brain.backend = "claude" | "codex"`` picks the Brain.

The orchestrator calls :func:`create_brain` with the value from the user config;
nothing else in Sombra knows which agent answers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Literal, get_args

from sombra.brain.claude import AnthropicClient, ClaudeBrain, ClaudeSettings
from sombra.brain.codex import CodexBrain, CodexSettings
from sombra.contracts import AutonomyLevel, Brain

BackendName = Literal["claude", "codex"]
BACKENDS: tuple[str, ...] = get_args(BackendName)
DEFAULT_BACKEND: BackendName = "claude"


def create_brain(
    backend: str,
    *,
    user_name: str,
    aliases: Sequence[str] = (),
    allowed_topics: Sequence[str] = (),
    level: AutonomyLevel = AutonomyLevel.L2,
    model: str | None = None,
    api_key: Callable[[], str] | None = None,
) -> Brain:
    """Build the configured backend. ``model=None`` keeps the backend's default.

    ``api_key`` is the keychain lookup for that backend's provider. Claude needs
    it; Codex falls back to the CLI's own login (``codex login``) without it.
    """
    if backend == "claude":
        if api_key is None:
            raise ValueError("the claude backend needs an API key lookup")
        claude = ClaudeSettings(user_name, aliases, allowed_topics, level)
        if model is not None:
            claude = replace(claude, model=model)
        return ClaudeBrain(claude, AnthropicClient(api_key))
    if backend == "codex":
        codex = CodexSettings(user_name, aliases, allowed_topics, level, model=model)
        return CodexBrain(codex, api_key=api_key)
    raise ValueError(f"unknown brain backend {backend!r}; expected one of {', '.join(BACKENDS)}")
