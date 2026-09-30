"""``sombra replay``: the whole pipeline on recorded files, for CI and offline tuning (#17).

Two WAV files stand in for the mic and the call audio (``audio.FileAudioSource``) and a
folder of ``<unix_ms>.png`` screenshots for the screen (``screen.DirectoryScreenSource``).
Everything after that is the live wiring: real VAD + whisper, dedupe, trigger detector,
store and :class:`Session`. The brain and the overlay can be the real ones or, for CI,
:class:`ScriptedBrain` (``--fake-brain``) and :class:`AutoApproveUI` (``--auto-approve``).

Time: sample 0 of both WAVs is the replay's start; screenshots keep their gaps and are
re-stamped onto the same clock, so the transcript reads as if the meeting ran now.
``speed=None`` runs as fast as the models allow; ``speed=1`` is real time, the only
speed at which ``latency_ms`` in ``log.jsonl`` means what it does in a live meeting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from sombra.audio import FileAudioSource
from sombra.config import BrainConfig
from sombra.contracts import (
    SAMPLE_RATE,
    ActionKind,
    ApprovalUI,
    AudioChunk,
    AudioDevice,
    AutonomyLevel,
    Brain,
    BrainRequest,
    BrainResponse,
    Screenshot,
    Suggestion,
    Transcriber,
    TriggerEvent,
    UserAction,
)
from sombra.orchestrator.session import Clock, local_now
from sombra.orchestrator.settings import SessionSettings
from sombra.orchestrator.wiring import (
    Assembly,
    NoBrain,
    SilentUI,
    build_agent_brain,
    build_detector,
    build_session,
    build_transcriber,
    epoch_interval_s,
)
from sombra.screen.replay import DirectoryScreenSource, ReplayExhaustedError
from sombra.store import create_meeting
from sombra.summary import TextModel

log = logging.getLogger(__name__)

SCRIPTED_BACKEND = "scripted"
SCRIPTED_MODEL = "replay-fake"
# Between two grabs; DirectoryScreenSource itself waits out the gaps between frames.
REPLAY_SCREEN_INTERVAL_S = 0.01
# Offline replay may transcribe much slower than real time: let stop() wait for it.
REPLAY_DRAIN_TIMEOUT_S = 600.0


# --- sources -------------------------------------------------------------------------


class PacedAudio:
    """An ``AudioSource`` that paces another one at ``speed`` x real time (None: no pacing)."""

    def __init__(self, inner: FileAudioSource, speed: float | None = None) -> None:
        if speed is not None and speed <= 0:
            raise ValueError("speed must be > 0")
        self.inner = inner
        self.speed = speed

    def list_devices(self) -> list[AudioDevice]:
        return self.inner.list_devices()

    async def stream(self) -> AsyncIterator[AudioChunk]:
        first: datetime | None = None
        t0 = time.monotonic()
        async for chunk in self.inner.stream():
            if self.speed is not None:
                first = first or chunk.start
                end = (chunk.start - first).total_seconds() + len(chunk.pcm_f32le) / 4 / SAMPLE_RATE
                await asyncio.sleep(max(0.0, end / self.speed - (time.monotonic() - t0)))
            yield chunk

    async def close(self) -> None:
        await self.inner.close()


class ReplayScreen:
    """``ScreenSource`` over a screenshot folder, re-stamped so frame 0 lands at ``start``.

    After the last screenshot ``grab()`` waits until cancelled instead of raising, so the
    session's screen pipeline does not crash-loop; :attr:`exhausted` is set then.
    """

    def __init__(self, inner: DirectoryScreenSource | None, start: datetime) -> None:
        self.inner = inner
        self.start = start
        self.exhausted = asyncio.Event()
        self._first: datetime | None = None
        if inner is None or len(inner) == 0:
            self.exhausted.set()

    async def grab(self) -> Screenshot:
        if self.inner is not None and not self.exhausted.is_set():
            try:
                shot = await self.inner.grab()
            except ReplayExhaustedError:
                self.exhausted.set()
            else:
                self._first = self._first or shot.ts
                return replace(shot, ts=self.start + (shot.ts - self._first))
        # Nothing more to show: wait for the session to cancel us.
        never: asyncio.Future[Screenshot] = asyncio.get_running_loop().create_future()
        return await never

    async def close(self) -> None:
        if self.inner is not None:
            await self.inner.close()


# --- stand-ins for the brain and the overlay (CI) --------------------------------------


class ScriptedBrain:
    """``Brain`` without a model: answers every trigger with a fixed PT-BR text.

    It reports every frame it was given as sent, so a replay can check that the
    frames reached the brain without calling an API.
    """

    backend = SCRIPTED_BACKEND

    def __init__(self) -> None:
        self.meeting_dir: Path | None = None
        self.requests: list[BrainRequest] = []

    async def start(self, meeting_dir: Path) -> None:
        self.meeting_dir = meeting_dir

    async def answer(self, request: BrainRequest) -> BrainResponse:
        self.requests.append(request)
        return BrainResponse(
            text=f"(resposta simulada) Sobre: {request.trigger.question}",
            frames_sent=[p.stem for p in request.frame_paths],
            backend=SCRIPTED_BACKEND,
            model=SCRIPTED_MODEL,
        )

    async def close(self) -> None:
        return None


class AutoApproveUI:
    """``ApprovalUI`` that approves every suggestion unedited, as soon as it is shown."""

    def __init__(self) -> None:
        self.notified: list[TriggerEvent] = []
        self.shown: list[Suggestion] = []
        self.failures: list[tuple[TriggerEvent, str]] = []
        self._queue: asyncio.Queue[UserAction] = asyncio.Queue()

    async def notify_trigger(self, trigger: TriggerEvent) -> None:
        self.notified.append(trigger)

    async def show(self, suggestion: Suggestion) -> None:
        self.shown.append(suggestion)
        log.info("auto-approved: %s", suggestion.text)
        self._queue.put_nowait(UserAction(suggestion_id=suggestion.id, kind=ActionKind.APPROVE))

    async def notify_failure(self, trigger: TriggerEvent, reason: str) -> None:
        self.failures.append((trigger, reason))
        log.warning("answer failed for %r: %s", trigger.question, reason)

    async def actions(self) -> AsyncIterator[UserAction]:
        while True:
            yield await self._queue.get()


# --- the run ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayOptions:
    me: Path | None
    others: Path | None
    meetings_root: Path
    user_name: str
    aliases: Sequence[str] = ()
    frames: Path | None = None
    speed: float | None = None  # None: as fast as possible
    name: str = "replay"
    level: AutonomyLevel = AutonomyLevel.L2
    allowed_topics: Sequence[str] = ()
    stt_model: str = "large-v3-turbo"
    models_dir: Path | None = None
    n_threads: int = 4
    use_gpu: bool = True
    agent_model: str = "sonnet"
    backend: str = "claude-api"  # brain.backend: claude-code | claude-api | codex
    auth: str | None = None  # brain.auth (codex: subscription | api-key)


def media_clock(start: datetime, speed: float) -> Clock:
    """Wall time mapped onto the replayed meeting's time line (``speed`` x faster)."""
    t0 = time.monotonic()
    return lambda: start + timedelta(seconds=(time.monotonic() - t0) * speed)


async def run_replay(
    opts: ReplayOptions,
    *,
    transcriber: Transcriber | None = None,
    brain: Brain | None = None,
    ui: ApprovalUI | None = None,
    summary_model: TextModel | None = None,
    api_key: Callable[[], str] | None = None,
) -> Path:
    """Replay the files into a new meeting folder under ``opts.meetings_root``; return it.

    ``brain`` defaults to ``opts.backend`` with ``api_key`` (required when the backend
    is paid with a key: ``claude-api``, or ``codex`` with ``auth = "api-key"``);
    ``ui`` is required (the overlay, or AutoApproveUI). Without
    ``summary_model`` no epoch summaries or minutes are written.
    """
    level = opts.level
    if level is AutonomyLevel.L3:
        raise ValueError("L3 (answering alone) is not available yet")
    names = [opts.user_name, *opts.aliases]
    if level is AutonomyLevel.L0:
        brain, ui = NoBrain(), SilentUI()
    if brain is None:
        if BrainConfig(opts.backend, opts.auth).uses_api_key and api_key is None:
            raise ValueError(f"the {opts.backend} brain needs an API key (or a scripted brain)")
        brain = build_agent_brain(
            opts.backend,
            api_key,
            user_name=opts.user_name,
            aliases=opts.aliases,
            allowed_topics=opts.allowed_topics,
            level=level,
            model=opts.agent_model,
        )
    if ui is None:
        raise ValueError("replay needs an ApprovalUI (the overlay, or AutoApproveUI)")
    if transcriber is None:
        transcriber = build_transcriber(
            opts.stt_model,
            vocabulary=[n for n in names if n],
            models_dir=opts.models_dir,
            n_threads=opts.n_threads,
            use_gpu=opts.use_gpu,
        )
    start = datetime.now().astimezone()
    meeting_dir = create_meeting(
        opts.meetings_root,
        opts.name,
        aliases=[n for n in (opts.user_name, *opts.aliases) if n],
        autonomy_level=level,
        allowed_topics=opts.allowed_topics,
        started_at=start,
    )
    audio = PacedAudio(FileAudioSource(opts.me, opts.others, start=start), opts.speed)
    frames = DirectoryScreenSource(opts.frames, speed=opts.speed) if opts.frames else None
    screen = ReplayScreen(frames, start)
    clock = media_clock(start, opts.speed) if opts.speed is not None else local_now
    on_epoch = getattr(brain, "start_epoch", None)
    assembly = build_session(
        meeting_dir,
        audio=audio,
        transcriber=transcriber,
        screen=screen,
        detector=build_detector(opts.user_name, opts.aliases),
        brain=brain,
        ui=ui,
        settings=SessionSettings(
            autonomy=level,
            screen_interval_s=REPLAY_SCREEN_INTERVAL_S,
            drain_timeout_s=REPLAY_DRAIN_TIMEOUT_S,
            epoch_interval_s=epoch_interval_s(),
        ),
        started_at=start,
        summary_model=summary_model,
        on_epoch=on_epoch if callable(on_epoch) else None,
        clock=clock,
    )
    await _run_to_end(assembly, screen)
    return meeting_dir


async def _run_to_end(assembly: Assembly, screen: ReplayScreen) -> None:
    """Run until both recordings are used up, then stop (drain, minutes, close)."""
    session = assembly.session
    task = asyncio.create_task(session.run(), name="replay-session")
    try:
        while "audio" not in session.pipelines:
            if task.done():
                await task  # run() failed before starting its pipelines
                return
            await asyncio.sleep(0)
        audio_task = session.pipelines["audio"].task
        assert audio_task is not None  # noqa: S101 - started by run()
        exhausted = asyncio.create_task(screen.exhausted.wait())
        try:
            await asyncio.wait({audio_task, task}, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.wait({exhausted, task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            exhausted.cancel()
        await session.stop()
        await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await assembly.close()
