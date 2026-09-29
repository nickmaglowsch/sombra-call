"""Missed-trigger review helper.

Lists ``OUTROS`` lines that mention one of the user's aliases but have no logged
trigger near them, for manual labelling. The missed-trigger rate itself is measured by
that review, not computed here: a mention is often not a question for the user.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sombra.contracts import Channel, SpeechLine, TriggerLogged
from sombra.metrics.reader import aware, meeting_day, read_log, read_transcript

DEFAULT_TOLERANCE_S = 30.0


@dataclass(frozen=True, slots=True)
class MissedTrigger:
    ts: datetime
    alias: str
    line: str  # the transcript line, as written

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "alias": self.alias, "line": self.line}


def missed_triggers(
    meeting_dir: Path,
    aliases: Iterable[str] = (),
    *,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> list[MissedTrigger]:
    """OUTROS lines naming an alias with no trigger within ``tolerance_s`` of the line.

    With no ``aliases``, the aliases that matched logged triggers are used.
    """
    log = read_log(meeting_dir)
    triggers = [e for e in log.events if isinstance(e, TriggerLogged)]
    aliases = [a for a in aliases if a.strip()] or sorted({t.matched_alias for t in triggers})
    patterns = [(a, _alias_re(a)) for a in aliases]
    if not patterns:
        return []
    trigger_times = [aware(t.ts) for t in triggers]
    day = meeting_day(meeting_dir, [e.ts for e in log.events])

    found: list[MissedTrigger] = []
    for entry in read_transcript(meeting_dir, day):
        if not isinstance(entry, SpeechLine) or entry.channel is not Channel.OTHERS:
            continue
        text = _fold(entry.text)
        alias = next((a for a, rx in patterns if rx.search(text)), None)
        if alias is None:
            continue
        ts = aware(entry.ts)
        if any(abs((t - ts).total_seconds()) <= tolerance_s for t in trigger_times):
            continue
        found.append(MissedTrigger(ts, alias, entry.to_line()))
    return found


def _fold(text: str) -> str:
    """Case- and accent-insensitive form: 'Nícolas' and 'nicolas' match."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _alias_re(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(_fold(alias.strip()))}(?!\w)")
