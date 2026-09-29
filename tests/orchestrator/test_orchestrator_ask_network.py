"""``sombra ask`` against the real API (``network``; never runs in CI).

Run by hand, with a key in the environment::

    ANTHROPIC_API_KEY=... uv run pytest -m network -s \
        tests/orchestrator/test_orchestrator_ask_network.py

The fixture meeting is synthetic (``ask_fixture``). Each answer must cite at least one
``[HH:MM:SS]`` and every cited time must exist in the transcript; the unanswerable
question must be answered with "não está na transcrição".
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from ask_fixture import make_meeting, transcript_times

from sombra.brain.prompt import ASK_NOT_FOUND
from sombra.config import UserConfig, UserIdentity
from sombra.orchestrator.ask import ask, claude_brain
from sombra.orchestrator.session import local_now

pytestmark = pytest.mark.network

TIME_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2})\]")

FACTUAL = [
    ("Qual prazo combinamos para o beta?", ("sexta", "3")),
    ("Quem vai cuidar da migração do banco?", ("Ana",)),
    ("Quanto foi o churn de setembro?", ("2,4",)),
]
UNANSWERABLE = "Qual orçamento foi aprovado para o marketing?"


@pytest.fixture
def meeting(tmp_path: Path) -> Path:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    return make_meeting(tmp_path / "meetings", "Daily time X")


def _cfg(meeting: Path) -> UserConfig:
    return UserConfig(meetings_root=meeting.parent, user=UserIdentity(name="Nick"))


@pytest.mark.parametrize(("question", "facts"), FACTUAL)
async def test_factual_questions_cite_real_timestamps(
    meeting: Path, question: str, facts: tuple[str, ...]
) -> None:
    brain = claude_brain(meeting, _cfg(meeting), frames=False)
    result = await ask(brain, meeting, question, frames=False, clock=local_now)
    text = result.response.text
    sys.stderr.write(
        f"\n{question}\n-> {text}\n({result.elapsed_s:.1f} s, {result.response.usage})\n"
    )
    cited = set(TIME_RE.findall(text))
    assert cited, "no [HH:MM:SS] reference"
    assert cited <= transcript_times(meeting), (
        f"invented times: {cited - transcript_times(meeting)}"
    )
    for fact in facts:
        assert fact in text
    assert ASK_NOT_FOUND.casefold() not in text.casefold()


async def test_unanswerable_question_says_not_in_transcript(meeting: Path) -> None:
    brain = claude_brain(meeting, _cfg(meeting), frames=False)
    result = await ask(brain, meeting, UNANSWERABLE, frames=False, clock=local_now)
    text = result.response.text
    sys.stderr.write(f"\n{UNANSWERABLE}\n-> {text}\n({result.elapsed_s:.1f} s)\n")
    assert "não está na transcrição" in text.casefold()
    assert set(TIME_RE.findall(text)) <= transcript_times(meeting)
