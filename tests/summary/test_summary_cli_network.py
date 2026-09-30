"""Live run of the subscription-CLI TextModels (``network``; never runs in CI).

Needs the CLI installed and logged in (``claude`` then ``/login``; ``codex login``)::

    SOMBRA_CLAUDE_LOGIN=1 uv run pytest -m network tests/summary/test_summary_cli_network.py -s
    SOMBRA_CODEX_LOGIN=1 uv run pytest -m network tests/summary/test_summary_cli_network.py -s
"""

import os
import shutil
import time

import pytest

from sombra.summary import ClaudeCliTextModel, CodexCliTextModel, TextModel

pytestmark = pytest.mark.network

SYSTEM = "Resuma a reunião em até 3 tópicos, em português."
USER = (
    "<transcricao>\n[14:00:00] OUTROS: o beta sai na sexta\n"
    "[14:00:05] EU: e a Ana fica com a migração do banco\n</transcricao>"
)


def _run(model: TextModel) -> None:
    t = time.monotonic()
    text, usage = model.complete(SYSTEM, USER, 400)
    print(f"\n{model.name}: {time.monotonic() - t:.2f} s, {usage}\n{text}")  # noqa: T201
    assert "sexta" in text.casefold()


def test_claude_cli_text_model() -> None:
    if shutil.which("claude") is None or os.environ.get("SOMBRA_CLAUDE_LOGIN") != "1":
        pytest.skip("set SOMBRA_CLAUDE_LOGIN=1 with a logged-in `claude` on PATH")
    _run(ClaudeCliTextModel(model=os.environ.get("SOMBRA_SUMMARY_MODEL") or None))


def test_codex_cli_text_model() -> None:
    if shutil.which("codex") is None or os.environ.get("SOMBRA_CODEX_LOGIN") != "1":
        pytest.skip("set SOMBRA_CODEX_LOGIN=1 with a logged-in `codex` on PATH")
    _run(CodexCliTextModel(model=os.environ.get("SOMBRA_CODEX_MODEL") or None))
