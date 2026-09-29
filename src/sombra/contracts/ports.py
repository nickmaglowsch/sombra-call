"""Interfaces between modules (ports). Implementations live in their own packages.

Modules depend only on ``sombra.contracts``; only ``sombra.orchestrator`` imports
concrete implementations and wires them together. That rule is what lets each
module be built and tested in parallel against fakes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from sombra.contracts.records import ActionKind, FrameRecord, LogEvent, Usage
from sombra.contracts.timeline import Channel, TimelineEntry

SAMPLE_RATE = 16_000  # Hz; what whisper.cpp and Silero VAD expect

# --- audio ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioDevice:
    id: str
    name: str
    is_input: bool  # mic-like source vs. system-output loopback/tap


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Mono float32 little-endian PCM at SAMPLE_RATE, one channel per chunk."""

    channel: Channel
    start: datetime
    pcm_f32le: bytes


class AudioSource(Protocol):
    def list_devices(self) -> list[AudioDevice]: ...

    def stream(self) -> AsyncIterator[AudioChunk]:
        """Yield chunks from mic (Channel.ME) and system audio (Channel.OTHERS) interleaved."""
        ...

    async def close(self) -> None: ...


# --- transcription -------------------------------------------------------------------


class Transcriber(Protocol):
    def transcribe(self, chunks: AsyncIterator[AudioChunk]) -> AsyncIterator[TimelineEntry]:
        """VAD-segment and transcribe; yield one SpeechLine per finished utterance."""
        ...


# --- screen --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Screenshot:
    ts: datetime
    image: bytes  # encoded PNG, full resolution
    app: str | None
    window_title: str | None


class ScreenSource(Protocol):
    async def grab(self) -> Screenshot: ...

    async def close(self) -> None: ...


class FramePipeline(Protocol):
    def process(self, shot: Screenshot) -> FrameRecord | None:
        """Dedupe, resize, save to disk. Return the record if the frame was kept, else None."""
        ...


# --- meeting storage -----------------------------------------------------------------


class TimelineStore(Protocol):
    """Single writer for a meeting folder. Every write is an append; nothing is rewritten."""

    @property
    def meeting_dir(self) -> Path: ...

    def append_entry(self, entry: TimelineEntry) -> None: ...

    def append_frame(self, record: FrameRecord) -> None: ...

    def log(self, event: LogEvent) -> None: ...

    def entries_since(self, since: datetime) -> list[TimelineEntry]: ...


# --- trigger -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerEvent:
    id: str
    ts: datetime  # end of the triggering utterance
    question: str
    matched_alias: str
    score: float  # 0..1 confidence of the name match + request structure
    window: Sequence[TimelineEntry]  # last ~60 s of timeline, oldest first
    needs_screen: bool  # deictic heuristic (phase 1) / classifier (phase 3)
    candidate_frames: Sequence[str] = ()  # 0-3 frame ids, most relevant first


class TriggerDetector(Protocol):
    def feed(self, entry: TimelineEntry) -> TriggerEvent | None: ...


# --- brain ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrainRequest:
    trigger: TriggerEvent
    frame_paths: Sequence[Path] = ()  # ephemeral tail images, max 3


@dataclass(frozen=True, slots=True)
class BrainResponse:
    text: str
    frames_sent: Sequence[str]
    backend: str
    model: str
    usage: Usage = field(default_factory=Usage)


class Brain(Protocol):
    """An agent backend (Claude Code via Agent SDK, Codex, ...). Read-only on the meeting dir."""

    async def start(self, meeting_dir: Path) -> None: ...

    async def answer(self, request: BrainRequest) -> BrainResponse: ...

    async def close(self) -> None: ...


# --- output --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Suggestion:
    id: str
    trigger_id: str
    text: str
    excerpt: str  # the transcript snippet that fired the trigger
    frames_sent: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class UserAction:
    suggestion_id: str
    kind: ActionKind
    text: str | None = None


@runtime_checkable
class ApprovalUI(Protocol):
    async def notify_trigger(self, trigger: TriggerEvent) -> None:
        """Early 'looking up context...' state while the brain works (O2)."""
        ...

    async def show(self, suggestion: Suggestion) -> None: ...

    async def notify_failure(self, trigger: TriggerEvent, reason: str) -> None:
        """Agent/API failed: show the excerpt so the user can answer manually."""
        ...

    def actions(self) -> AsyncIterator[UserAction]: ...
