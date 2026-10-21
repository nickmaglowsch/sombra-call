"""Real model run on the synthetic 30 min PT-BR fixture. Never runs in CI.

With an API key::

    ANTHROPIC_API_KEY=... uv run pytest -m network tests/summary/test_summary_network.py -s

or, with no key, on the logged-in ``claude`` CLI (Claude Pro/Max; see ``claude_backend``)::

    SOMBRA_CLAUDE_LOGIN=1 uv run pytest -m network tests/summary/test_summary_network.py -s

On the CLI login the minutes and the epoch are checked the same way; the cost test skips,
since the subscription isn't billed per token, after printing the token counts.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from claude_backend import COST_NOT_OBSERVABLE, Backend, api_key, claude_backend, env_model
from sombra.summary import ClaudeCliTextModel, TextModel
from sombra.summary.epochs import EpochSummarizer
from sombra.summary.minutes import write_minutes
from sombra.summary.model import DEFAULT_MODEL, AnthropicTextModel
from sombra.summary.summary_file import read_minutes_section
from sombra.summary.transcript import read_transcript

pytestmark = pytest.mark.network

FIXTURE = Path(__file__).parent / "fixtures" / "daily_30min_ptbr.md"
DAY = datetime(2026, 9, 29).astimezone()
# USD per 1M tokens (input, output) for the default model; update with the model.
PRICES = {"claude-haiku-4-5": (1.00, 5.00)}


@dataclass(frozen=True)
class Run:
    backend: Backend
    model: str
    epoch_words: int
    minutes: str | None
    actions: int
    dropped: int
    tokens_in: int
    tokens_out: int


def _model(backend: Backend) -> TextModel:
    if backend == "api":
        return AnthropicTextModel(api_key, model=env_model("SOMBRA_SUMMARY_MODEL") or DEFAULT_MODEL)
    return ClaudeCliTextModel(model=env_model("SOMBRA_SUMMARY_MODEL"))


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """One epoch and one minutes call on the fixture, shared by the tests below."""
    backend = claude_backend()
    model = _model(backend)
    folder = tmp_path_factory.mktemp("meeting")
    (folder / "transcript.md").write_bytes(FIXTURE.read_bytes())

    lines = read_transcript(folder / "transcript.md", day=DAY)
    epoch = EpochSummarizer(model, started_at=lines[0].entry.ts).summarize(
        [ln.entry for ln in lines], lines[-1].entry.ts
    )
    result = write_minutes(folder, model, day=DAY)
    ata = read_minutes_section(folder / "summary.md")
    tokens_in = epoch.event.usage.input_tokens + result.usage.input_tokens
    tokens_out = epoch.event.usage.output_tokens + result.usage.output_tokens
    print(  # noqa: T201 - manual run reports what it measured for the PR
        f"\nbackend={backend} model={model.name} in={tokens_in} out={tokens_out} "
        f"dropped={len(result.dropped)}\n\n{ata}"
    )
    return Run(
        backend=backend,
        model=model.name,
        epoch_words=len(epoch.text.split()),
        minutes=ata,
        actions=len(result.minutes.actions),
        dropped=len(result.dropped),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def test_minutes_and_epoch_on_30min_fixture(run: Run) -> None:
    assert run.epoch_words <= 600
    assert run.minutes and "### Itens de ação" in run.minutes
    assert run.actions >= 3  # the fixture has ~6 explicit commitments


def test_cost_on_30min_fixture(run: Run) -> None:
    if run.backend == "cli":
        pytest.skip(COST_NOT_OBSERVABLE)
    in_price, out_price = PRICES.get(run.model, (0.0, 0.0))
    cost = (run.tokens_in * in_price + run.tokens_out * out_price) / 1e6
    print(f"\nmodel={run.model} cost=${cost:.4f}")  # noqa: T201 - reports the cost for the PR
