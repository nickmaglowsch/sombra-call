"""``[HH:MM:SS]`` citations in a ``sombra ask`` answer, checked against ``transcript.md`` (#77).

The answer must cite only times that start a line of the transcript (PRD: answers cite
their sources). The rule is the minutes' one (#12, ``summary.minutes.validate_actions``):
a time is real when :func:`~sombra.summary.minutes.normalize_hms` of it is the
``HH:MM:SS`` of a line :func:`~sombra.summary.transcript.read_transcript` parses, speech
and ``TELA`` lines alike.

An invalid citation is **dropped**, as the minutes drop an action item with an invented
time. Replacing it with the nearest real time would print a precise-looking source the
model never chose, and marking it would still show the user a time that does not exist.
A bracket that cites several times (``[14:30:05, 00:00:00]``) keeps its real ones; a
bracket with no real time is removed with one of the spaces around it. Brackets whose times are
all real are left byte for byte as the model wrote them. Only bracketed times are
citations; a time written in prose (``às 14h30``) is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sombra.summary.minutes import normalize_hms
from sombra.summary.transcript import read_transcript

_TIME = r"\d{1,2}:\d{2}:\d{2}"
_TIME_RE = re.compile(_TIME)
# A bracket holding one or more times, joined by commas, dashes or "e"/"a"/"até".
_CITATION_RE = re.compile(
    rf"(?P<space>[ \t]*)"
    rf"\[(?P<inner>\s*{_TIME}(?:\s*(?:[,;–—-]|e|a|até)\s*{_TIME})*\s*)\]"
    r"(?P<after>[ \t]?)"
)


@dataclass(frozen=True, slots=True)
class CheckedAnswer:
    text: str
    dropped: tuple[str, ...]  # invalid times, in the order the model cited them


def transcript_times(meeting_dir: Path, *, day: datetime) -> list[str]:
    """``HH:MM:SS`` of every timeline line of ``transcript.md``, in order (empty if unreadable)."""
    try:
        lines = read_transcript(Path(meeting_dir) / "transcript.md", day=day)
    except (OSError, UnicodeDecodeError):
        return []
    return [line.hms for line in lines]


def check_citations(text: str, known: set[str]) -> CheckedAnswer:
    """``text`` with every cited time that is not in ``known`` dropped."""
    dropped: list[str] = []

    def fix(m: re.Match[str]) -> str:
        times = _TIME_RE.findall(m["inner"])
        real = [t for t in times if normalize_hms(t) in known]
        dropped.extend(t for t in times if normalize_hms(t) not in known)
        if len(real) == len(times):
            return m[0]
        if not real:  # drop one of the spaces around it, not both
            return m["after"] if m["space"] else ""
        return f"{m['space']}[{', '.join(real)}]{m['after']}"

    checked = _CITATION_RE.sub(fix, text)
    return CheckedAnswer(checked, tuple(dropped))


def clock_hint(times: Sequence[str]) -> str | None:
    """A line for the question's tail giving the transcript's real clock range, or None."""
    if not times:
        return None
    return (
        f"Os horários da transcrição são horas do relógio, de [{times[0]}] a "
        f"[{times[-1]}]; não contam a partir de 00:00:00. Cite só horários de linhas "
        "que existem na transcrição e, se não houver uma linha para citar, não cite "
        "horário nenhum: horários que não existem na transcrição são removidos da resposta."
    )
