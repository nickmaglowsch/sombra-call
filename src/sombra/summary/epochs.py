"""Rolling summary epochs (C4).

Every ``epoch_minutes`` (default 25, allowed 20–30) the orchestrator asks for a new
compact summary of the whole meeting: previous summary + transcript since the last
boundary in, ≤ ~400 words out. Each rewrite changes the prompt prefix once (a planned
cache miss); between boundaries the summary, and therefore the prefix, stays fixed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sombra.contracts import SummaryEpochLogged, TimelineEntry, Usage
from sombra.summary import prompts
from sombra.summary.model import DEFAULT_CONTEXT_TOKENS, TextModel, add_usage
from sombra.summary.summary_file import EpochSection
from sombra.summary.transcript import chunk_lines, estimate_tokens, render, to_lines

log = logging.getLogger(__name__)

MIN_EPOCH_MINUTES = 20
MAX_EPOCH_MINUTES = 30
DEFAULT_EPOCH_MINUTES = 25
DEFAULT_MAX_WORDS = 400


@dataclass(frozen=True, slots=True)
class EpochResult:
    text: str
    event: SummaryEpochLogged
    section: EpochSection  # what to append to summary.md


class EpochSummarizer:
    """Produces one summary per epoch; holds the current summary between epochs.

    Pass ``previous`` (from ``summary_file.read_current_epoch``) to resume after a
    restart. Epoch boundaries are counted from ``started_at``, or from the previous
    epoch's end when resuming.
    """

    def __init__(
        self,
        model: TextModel,
        *,
        started_at: datetime,
        epoch_minutes: int = DEFAULT_EPOCH_MINUTES,
        max_words: int = DEFAULT_MAX_WORDS,
        context_tokens: int = DEFAULT_CONTEXT_TOKENS,
        previous: EpochSection | None = None,
        previous_at: datetime | None = None,
    ) -> None:
        if not MIN_EPOCH_MINUTES <= epoch_minutes <= MAX_EPOCH_MINUTES:
            raise ValueError(
                f"epoch_minutes must be {MIN_EPOCH_MINUTES}-{MAX_EPOCH_MINUTES}, "
                f"got {epoch_minutes}"
            )
        self._model = model
        self._period = timedelta(minutes=epoch_minutes)
        self._max_words = max_words
        self._system = prompts.EPOCH_SYSTEM.format(max_words=max_words)
        # Leave room for system prompt, previous summary and the answer.
        self._chunk_tokens = max(1_000, int(context_tokens * 0.6))
        self._max_tokens = max(512, max_words * 4)
        self._epoch = previous.epoch if previous else 0
        self._summary = previous.text if previous else ""
        self._last_boundary = previous_at if previous and previous_at else started_at

    @property
    def epoch(self) -> int:
        """Number of the last completed epoch (0 = none yet)."""
        return self._epoch

    @property
    def summary(self) -> str:
        """The current summary; this is what goes in the stable prompt prefix."""
        return self._summary

    @property
    def next_boundary(self) -> datetime:
        return self._last_boundary + self._period

    def due(self, now: datetime) -> bool:
        return now >= self.next_boundary

    def summarize(self, entries: Sequence[TimelineEntry], now: datetime) -> EpochResult:
        """Rewrite the summary with ``entries`` (the timeline since the last boundary).

        Long input is folded chunk by chunk, each call carrying the running summary.
        On a model error nothing changes (the caller logs it and keeps recording).
        """
        lines = to_lines(entries)
        summary = self._summary
        usages: list[Usage] = []
        budget = max(1, self._chunk_tokens - estimate_tokens(summary))
        for chunk in chunk_lines(lines, budget) or [[]]:
            text, usage = self._model.complete(
                self._system, prompts.epoch_user(summary, render(chunk)), self._max_tokens
            )
            summary = _cap_words(text.strip(), self._max_words)
            usages.append(usage)

        self._epoch += 1
        self._summary = summary
        self._last_boundary = now
        until = f"{lines[-1].entry.ts:%H:%M:%S}" if lines else f"{now:%H:%M:%S}"
        event = SummaryEpochLogged(
            ts=now, epoch=self._epoch, model=self._model.name, usage=add_usage(usages)
        )
        return EpochResult(summary, event, EpochSection(self._epoch, until, summary))


def _cap_words(text: str, max_words: int) -> str:
    """Hard cap at 1.5x the requested length; the prompt asks for ``max_words``."""
    limit = int(max_words * 1.5)
    words = list(re.finditer(r"\S+", text))
    if len(words) <= limit:
        return text
    log.warning("epoch summary had %d words, truncated to %d", len(words), limit)
    return text[: words[limit - 1].end()] + " …"
