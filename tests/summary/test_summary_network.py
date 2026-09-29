"""Real API run on the synthetic 30 min PT-BR fixture. Needs a key; never runs in CI.

ANTHROPIC_API_KEY=... uv run pytest -m network tests/summary/test_summary_network.py -s
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from sombra.summary.epochs import EpochSummarizer
from sombra.summary.minutes import write_minutes
from sombra.summary.model import DEFAULT_MODEL, AnthropicTextModel
from sombra.summary.summary_file import read_minutes_section
from sombra.summary.transcript import read_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "daily_30min_ptbr.md"
DAY = datetime(2026, 9, 29).astimezone()
# USD per 1M tokens (input, output) for the default model; update with the model.
PRICES = {"claude-haiku-4-5": (1.00, 5.00)}


def _key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    return key


@pytest.mark.network
def test_minutes_and_epoch_on_30min_fixture(tmp_path: Path) -> None:
    model_id = os.environ.get("SOMBRA_SUMMARY_MODEL", DEFAULT_MODEL)
    model = AnthropicTextModel(_key, model=model_id)
    shutil.copy(FIXTURE, tmp_path / "transcript.md")

    lines = read_transcript(tmp_path / "transcript.md", day=DAY)
    epoch = EpochSummarizer(model, started_at=lines[0].entry.ts).summarize(
        [ln.entry for ln in lines], lines[-1].entry.ts
    )
    assert len(epoch.text.split()) <= 600

    result = write_minutes(tmp_path, model, day=DAY)
    ata = read_minutes_section(tmp_path / "summary.md")
    assert ata and "### Itens de ação" in ata
    assert len(result.minutes.actions) >= 3  # the fixture has ~6 explicit commitments

    in_price, out_price = PRICES.get(model_id, (0.0, 0.0))
    total_in = epoch.event.usage.input_tokens + result.usage.input_tokens
    total_out = epoch.event.usage.output_tokens + result.usage.output_tokens
    cost = (total_in * in_price + total_out * out_price) / 1e6
    print(  # noqa: T201 - manual run reports the cost for the PR
        f"\nmodel={model_id} in={total_in} out={total_out} cost=${cost:.4f} "
        f"dropped={len(result.dropped)}\n\n{ata}"
    )
