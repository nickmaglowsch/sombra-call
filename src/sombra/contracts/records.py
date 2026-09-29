"""Records persisted as JSON lines: ``frames/index.jsonl`` and ``log.jsonl``.

Every record serialises to a flat JSON object with a ``type`` discriminator (log
events) and ISO-8601 timestamps. Readers must ignore unknown keys so fields can be
added without a migration; removing or renaming a field is a contract change.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar


def _default(o: object) -> object:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, StrEnum):
        return o.value
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def to_json_line(record: object) -> str:
    """Serialise a record dataclass to one JSON line (no trailing newline)."""
    data = asdict(record)  # type: ignore[call-overload]
    if (kind := getattr(record, "TYPE", None)) is not None:
        data = {"type": kind, **data}
    return json.dumps(data, default=_default, ensure_ascii=False, separators=(",", ":"))


# --- frames/index.jsonl ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrameRecord:
    """One kept (post-dedupe) frame. ``path`` is relative to the meeting dir."""

    id: str  # "f0123", see contracts.timeline.format_frame_id
    ts: datetime
    path: str  # "frames/f0123.jpg"
    width: int
    height: int
    app: str | None  # None where the platform cannot tell (Wayland)
    window_title: str | None
    diff_score: float  # dHash Hamming distance to the previous kept frame, 0..64
    phash: str  # hex dHash of this frame


# --- log.jsonl -----------------------------------------------------------------------


class AutonomyLevel(StrEnum):
    L0 = "L0"  # record only; minutes at the end
    L1 = "L1"  # private copilot suggestions
    L2 = "L2"  # answers when called, after user approval
    L3 = "L3"  # answers alone (phase 4, gated)


class ActionKind(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    DISCARD = "discard"
    NOT_FOR_ME = "not_for_me"  # discard + marks a false trigger (success metric)


@dataclass(frozen=True, slots=True)
class TriggerLogged:
    TYPE: ClassVar[str] = "trigger"
    trigger_id: str
    ts: datetime  # when the triggering utterance ended
    detected_at: datetime
    question: str
    matched_alias: str
    score: float
    needs_screen: bool


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class SuggestionLogged:
    TYPE: ClassVar[str] = "suggestion"
    suggestion_id: str
    trigger_id: str
    ts: datetime  # when it reached the overlay
    text: str
    frames_sent: list[str]
    backend: str  # "claude-agent-sdk", "codex", ...
    model: str
    latency_ms: int  # end of question -> shown in overlay
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class ActionLogged:
    TYPE: ClassVar[str] = "action"
    suggestion_id: str
    ts: datetime
    kind: ActionKind
    final_text: str | None = None  # the edited text for EDIT; the approved text for APPROVE


@dataclass(frozen=True, slots=True)
class AgentErrorLogged:
    TYPE: ClassVar[str] = "agent_error"
    trigger_id: str
    ts: datetime
    error: str


@dataclass(frozen=True, slots=True)
class SummaryEpochLogged:
    TYPE: ClassVar[str] = "summary_epoch"
    ts: datetime
    epoch: int
    model: str
    usage: Usage = field(default_factory=Usage)


LogEvent = TriggerLogged | SuggestionLogged | ActionLogged | AgentErrorLogged | SummaryEpochLogged


def log_event_type(line: str) -> str:
    """Cheap discriminator for readers that only need some event types."""
    t: Any = json.loads(line).get("type")
    if not isinstance(t, str):
        raise ValueError("log line has no string 'type'")
    return t
