"""Meeting folder and the single append-only writer for it (PRD M1, T3, S6 writing).

``create_meeting`` makes a meeting folder from the template in ``docs/ARCHITECTURE.md``;
``MeetingStore`` implements ``contracts.TimelineStore`` and is the only code that
writes ``transcript.md``, ``frames/index.jsonl`` and ``log.jsonl``.

This package takes plain arguments; it never reads the user config itself.
"""

from sombra.store.meeting import MeetingInfo, create_meeting, read_started_at, slugify
from sombra.store.timeline import MeetingStore

__all__ = ["MeetingInfo", "MeetingStore", "create_meeting", "read_started_at", "slugify"]
