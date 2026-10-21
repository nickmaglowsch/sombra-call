"""Which Claude backend a ``network`` test runs on: the API key or the ``claude`` login (#89).

The API-key network tests (#9, #12, #20/#77) also run on a Claude Pro/Max subscription
through the logged-in Claude Code CLI, opted into with ``SOMBRA_CLAUDE_LOGIN=1`` (the
same switch as ``SOMBRA_CODEX_LOGIN`` for Codex). The rules:

1. ``ANTHROPIC_API_KEY`` set: the API, exactly as before.
2. No key, ``SOMBRA_CLAUDE_LOGIN=1`` and ``claude`` on ``PATH``: the CLI login.
3. Otherwise the test skips and says which of the two is missing.

Some numbers the API reports can't be checked through the CLI; those assertions skip
with :data:`CACHE_NOT_COMPARABLE` / :data:`COST_NOT_OBSERVABLE` instead of faking them.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from typing import Literal

import pytest

Backend = Literal["api", "cli"]

NO_KEY = "ANTHROPIC_API_KEY not set (or set SOMBRA_CLAUDE_LOGIN=1 to use your `claude` login)"
NO_CLI = "SOMBRA_CLAUDE_LOGIN=1 but the claude CLI is not on PATH"

CACHE_NOT_COMPARABLE = (
    "cache-hit assertion not checked on the claude login: the CLI places its own cache "
    "breakpoints and adds its own prompt, so its hit rate (printed above) says nothing "
    "about ClaudeBrain's cache layout; run with ANTHROPIC_API_KEY for #9"
)
COST_NOT_OBSERVABLE = (
    "USD cost not observable on the claude login: the subscription is not billed per "
    "token and the CLI text model reports tokens only (printed above); run with "
    "ANTHROPIC_API_KEY for the cost"
)


def claude_backend(
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Backend:
    """The backend this run uses, or skip the test with the reason."""
    env = os.environ if environ is None else environ
    if env.get("ANTHROPIC_API_KEY"):
        return "api"
    if env.get("SOMBRA_CLAUDE_LOGIN") != "1":
        pytest.skip(NO_KEY)
    if which("claude") is None:
        pytest.skip(NO_CLI)
    return "cli"


def api_key() -> str:
    """The key for the ``api`` backend (call only after :func:`claude_backend`)."""
    return os.environ["ANTHROPIC_API_KEY"]


def env_model(name: str = "SOMBRA_CLAUDE_CODE_MODEL") -> str | None:
    """The model ``$name`` picks, or ``None`` (the backend's default)."""
    return os.environ.get(name) or None
