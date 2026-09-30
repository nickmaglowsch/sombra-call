"""``[HH:MM:SS]`` citations in a ``sombra ask`` answer, checked against ``transcript.md`` (#77).

The answer must cite only times that start a line of the transcript (PRD: answers cite
their sources). The rule is the minutes' one (#12, ``summary.minutes.validate_actions``):
a time is real when :func:`~sombra.summary.minutes.normalize_hms` of it is the
``HH:MM:SS`` of a line :func:`~sombra.summary.transcript.read_transcript` parses, speech
and ``TELA`` lines alike.

A citation is any ``[...]`` holding a time, including the ones that also name a screen,
as the ``--frames`` prompt asks (``[14:32:10, f0001]``), and link text
(``[00:00:00](...)``); ``[TELA f0001]`` is not one. An invalid time is **dropped**, as
the minutes drop an action item with an invented time. Replacing it with the nearest
real time would print a precise-looking source the model never chose, and marking it
would still show the user a time that does not exist.

- ``[[...]]`` counts as one bracket; a bracket may span lines.
- A bracket whose times are all real is left byte for byte as the model wrote it.
- Otherwise each invalid time goes with its separator and the rest stays:
  ``[14:30:05, 00:00:00]`` -> ``[14:30:05]``, ``[00:00:00, f0003]`` -> ``[f0003]``. A
  range with one invalid end keeps the real end only (``[00:00:00-14:33:00]`` ->
  ``[14:33:00]``): a point the transcript has, rather than a range it does not.
- A bracket left with no time and no frame id (``[00:00:00]``, ``[00:00:00 TELA]``) is
  removed with one of the spaces around it, and with emphasis around it (``**[..]**``).

Only bracketed times are citations; a time written in prose (``às 14h30``) is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sombra.summary.minutes import normalize_hms
from sombra.summary.transcript import read_transcript

_TIME_RE = re.compile(r"\d{1,2}:\d{2}:\d{2}")
_FRAME_RE = re.compile(r"f\d{4,}")  # contracts.timeline.FRAME_ID_RE, unanchored
# One bracket (no nesting; it may span lines), with the space and emphasis
# around it so a dropped citation leaves neither "x ." nor "****" behind.
_BRACKET_RE = re.compile(
    r"(?P<space>[ \t]*)(?P<em>\*\*|__|\*|_|`)?"
    r"(?P<outer>\[)?\[(?P<inner>[^\[\]]*)\](?(outer)\])"
    r"(?(em)(?P=em))(?P<after>[ \t]?)"
)
# What joins the items of a citation: punctuation, "e"/"a"/"até", or plain spaces.
_SEP_RE = re.compile(r"(\s*[,;/–—]\s*|\s*(?<=\d)-(?=\d)\s*|\s+-\s+|\s+(?:e|a|até)\s+|\s+)")


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

    def invalid(item: str) -> list[str]:
        return [t for t in _TIME_RE.findall(item) if normalize_hms(t) not in known]

    def fix(m: re.Match[str]) -> str:
        inner = m["inner"]
        if not _TIME_RE.search(inner):
            return m[0]  # not a citation
        parts = _SEP_RE.split(inner.strip())
        items, seps = parts[0::2], parts[1::2]
        bad = [invalid(item) for item in items]
        if not any(bad):
            return m[0]
        for times in bad:
            dropped.extend(times)
        kept = ""
        for i, item in enumerate(items):
            if bad[i]:
                continue
            if kept:  # the separator right before this item joins it to what is kept
                kept += seps[i - 1] if i > 0 and not bad[i - 1] else ", "
            kept += item
        if not (_TIME_RE.search(kept) or _FRAME_RE.search(kept)):
            return m["after"] if m["space"] else ""  # one of the spaces around it, not both
        em, outer = m["em"] or "", m["outer"] or ""
        return f"{m['space']}{em}{outer}[{kept}]{']' if outer else ''}{em}{m['after']}"

    checked = _BRACKET_RE.sub(fix, text)
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
