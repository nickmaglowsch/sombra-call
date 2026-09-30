"""One meeting, end to end: capture pipelines, trigger handling, epochs and shutdown.

Pipelines (each a supervised asyncio task, see :mod:`.supervisor`)::

    audio      AudioSource.stream ─(pause gate)─► audio queue
    transcribe audio queue ─► Transcriber ─► timeline queue
    screen     every N s ─(pause gate)─► grab ─(blocked app)─► FramePipeline
                          ─► store.append_frame (index only) ─► FrameMarker ─► timeline queue
    timeline   timeline queue ─► store.append_entry ─► TriggerDetector.feed
    triggers   one TriggerEvent at a time (max 1 pending) ─► UI + Brain
    actions    ApprovalUI.actions ─► ActionLogged
    epochs     every N min ─► summarizer ─► epoch hook ─► SummaryEpochLogged

The timeline queue has a single consumer, so the transcript has exactly one writer
and speech lines and screen markers keep their arrival order. Queues are bounded:
a slow consumer applies backpressure instead of growing memory.

Failure rules (PRD "Confiabilidade"): an agent error or timeout is logged as
``AgentErrorLogged`` and shown via ``ApprovalUI.notify_failure``, and never touches
the capture pipelines. A crashed pipeline is restarted with backoff while the
others keep running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path

from sombra.contracts import (
    ActionKind,
    ActionLogged,
    AgentErrorLogged,
    ApprovalUI,
    AudioChunk,
    AudioSource,
    AutonomyLevel,
    Brain,
    BrainRequest,
    BrainResponse,
    FrameMarker,
    FramePipeline,
    FrameRecord,
    ScreenSource,
    SpeechLine,
    Suggestion,
    SuggestionLogged,
    SummaryEpochLogged,
    TimelineEntry,
    TimelineStore,
    Transcriber,
    TriggerDetector,
    TriggerEvent,
    TriggerLogged,
)
from sombra.orchestrator.hooks import (
    BlockedApp,
    EpochHook,
    FrameResolver,
    MinutesWriter,
    PauseState,
    PauseSwitch,
    Summarizer,
    never_blocked,
)
from sombra.orchestrator.settings import SessionSettings
from sombra.orchestrator.supervisor import Supervised, TaskFactory

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

AUDIO = "audio"
TRANSCRIBE = "transcribe"
SCREEN = "screen"
TIMELINE = "timeline"
TRIGGERS = "triggers"
ACTIONS = "actions"
EPOCHS = "epochs"


def local_now() -> datetime:
    """Timezone-aware local wall clock (the transcript uses local ``HH:MM:SS``)."""
    return datetime.now().astimezone()


class Session:
    """Runs one meeting against the ports in ``sombra.contracts``.

    ``run()`` starts every pipeline and returns once ``stop()`` was called (or the
    task running it was cancelled, e.g. by Ctrl+C) and shutdown finished.
    """

    def __init__(
        self,
        *,
        audio: AudioSource,
        transcriber: Transcriber,
        screen: ScreenSource,
        frames: FramePipeline,
        store: TimelineStore,
        detector: TriggerDetector,
        brain: Brain,
        ui: ApprovalUI,
        settings: SessionSettings | None = None,
        summarizer: Summarizer | None = None,
        epoch_hook: EpochHook | None = None,
        minutes: MinutesWriter | None = None,
        pause: PauseState | None = None,
        blocked: BlockedApp = never_blocked,
        resolve_frame: FrameResolver | None = None,
        clock: Clock = local_now,
    ) -> None:
        self._audio = audio
        self._transcriber = transcriber
        self._screen = screen
        self._frame_pipeline = frames
        self._store = store
        self._detector = detector
        self._brain = brain
        self._ui = ui
        self.settings = settings or SessionSettings()
        self._summarizer = summarizer
        self._epoch_hook = epoch_hook
        self._minutes = minutes
        self._pause: PauseState = pause if pause is not None else PauseSwitch()
        self._blocked = blocked
        self._resolve_frame = resolve_frame or self._default_frame_path
        self._now = clock

        size = self.settings.queue_size
        self._audio_q: asyncio.Queue[AudioChunk | None] = asyncio.Queue(size)
        self._timeline_q: asyncio.Queue[TimelineEntry | None] = asyncio.Queue(size)
        self._trigger_q: asyncio.Queue[TriggerEvent | None] = asyncio.Queue(1)  # max 1 pending
        self._audio_done = False

        self._frames: dict[str, FrameRecord] = {}
        self._suggestions: dict[str, str] = {}
        self._pipelines: dict[str, Supervised] = {}
        self._brain_start: asyncio.Task[None] | None = None
        self._stop_requested = asyncio.Event()
        self._done = asyncio.Event()
        self._started = False
        self._shutting_down = False

        self.epoch = 0
        self.dropped_triggers = 0
        self.dropped_paused = 0  # audio chunks and speech lines discarded while paused

    # --- lifecycle ---------------------------------------------------------------------

    @property
    def pipelines(self) -> Mapping[str, Supervised]:
        return self._pipelines

    @property
    def paused(self) -> bool:
        return self._pause.is_paused()

    async def run(self) -> None:
        if self._started:
            raise RuntimeError("a Session runs once")
        self._started = True
        try:
            self._start_pipelines()
            if self.settings.autonomy is not AutonomyLevel.L0:
                # Starting an agent can take seconds; capture must not wait for it.
                self._brain_start = asyncio.create_task(
                    self._brain.start(self._store.meeting_dir), name="brain-start"
                )
            await self._stop_requested.wait()
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        """Drain the queues, write the minutes, close every port. Idempotent."""
        self._stop_requested.set()
        if self._started:
            await self._done.wait()

    def _start_pipelines(self) -> None:
        factories: dict[str, TaskFactory] = {
            AUDIO: self._capture_audio,
            TRANSCRIBE: self._transcribe,
            SCREEN: self._capture_screen,
            TIMELINE: self._write_timeline,
            TRIGGERS: self._answer_triggers,
            ACTIONS: self._log_actions,
        }
        if self._summarizer is not None:
            factories[EPOCHS] = self._run_epochs
        for name, factory in factories.items():
            sup = Supervised(
                name,
                factory,
                backoff_s=self.settings.restart_backoff_s,
                backoff_max_s=self.settings.restart_backoff_max_s,
            )
            self._pipelines[name] = sup
            sup.start()

    async def _shutdown(self) -> None:
        if self._shutting_down:
            await self._done.wait()
            return
        self._shutting_down = True
        try:
            # Sources first, so nothing new enters the queues.
            await self._cancel(AUDIO, SCREEN, EPOCHS)
            try:
                await asyncio.wait_for(self._drain(), self.settings.drain_timeout_s)
            except TimeoutError:
                log.warning("stop: queues not drained after %.1f s", self.settings.drain_timeout_s)
            await self._cancel(*self._pipelines)
            if self._brain_start is not None and not self._brain_start.done():
                self._brain_start.cancel()
            if self._minutes is not None:
                await self._guard("minutes", self._minutes())
            await self._guard("audio.close", self._audio.close())
            await self._guard("screen.close", self._screen.close())
            if self._brain_start is not None:
                await asyncio.gather(self._brain_start, return_exceptions=True)
                await self._guard("brain.close", self._brain.close())
        finally:
            self._done.set()

    async def _drain(self) -> None:
        if not self._finished(TRANSCRIBE):  # else nobody would take the sentinel
            await self._audio_q.put(None)
        await self._wait(TRANSCRIBE)
        await self._timeline_q.put(None)
        await self._wait(TIMELINE)
        await self._trigger_q.put(None)
        await self._wait(TRIGGERS)

    def _finished(self, name: str) -> bool:
        task = self._pipelines[name].task
        return task is None or task.done()

    async def _wait(self, name: str) -> None:
        task = self._pipelines[name].task
        if task is not None:
            await task

    async def _cancel(self, *names: str) -> None:
        tasks = [
            t for n in names if n in self._pipelines if (t := self._pipelines[n].task) is not None
        ]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _guard(what: str, aw: Awaitable[None]) -> bool:
        try:
            await aw
        except Exception:
            log.exception("%s failed", what)
            return False
        return True

    # --- capture pipelines -------------------------------------------------------------

    async def _capture_audio(self) -> None:
        stream = self._audio.stream()
        try:
            async for chunk in stream:
                if self._pause.is_paused():
                    self.dropped_paused += 1
                    continue
                await self._audio_q.put(chunk)
        finally:
            await _aclose(stream)

    async def _chunks(self) -> AsyncIterator[AudioChunk]:
        while not self._audio_done:
            chunk = await self._audio_q.get()
            if chunk is None:
                self._audio_done = True
                return
            yield chunk

    async def _transcribe(self) -> None:
        entries = self._transcriber.transcribe(self._chunks())
        try:
            async for entry in entries:
                await self._timeline_q.put(entry)
        finally:
            await _aclose(entries)

    async def _capture_screen(self) -> None:
        while True:
            await asyncio.sleep(self.settings.screen_interval_s)
            if self._pause.is_paused():
                continue
            shot = await self._screen.grab()
            if self._pause.is_paused() or self._blocked(shot.app, shot.window_title):
                continue
            # dHash + resize + JPEG encode is CPU work: keep it off the event loop.
            record = await asyncio.to_thread(self._frame_pipeline.process, shot)
            if record is None:
                continue
            self._store.append_frame(record)
            self._frames[record.id] = record
            title = record.window_title or record.app or ""
            await self._timeline_q.put(FrameMarker(record.ts, record.id, title))

    async def _write_timeline(self) -> None:
        while (entry := await self._timeline_q.get()) is not None:
            if isinstance(entry, SpeechLine) and self._pause.is_paused():
                # Speech finished transcribing after the user hit pause: not recorded.
                self.dropped_paused += 1
                continue
            self._store.append_entry(entry)
            event = self._detector.feed(entry)
            if event is not None:
                self._on_trigger(event)

    # --- triggers ----------------------------------------------------------------------

    def _on_trigger(self, event: TriggerEvent) -> None:
        self._store.log(
            TriggerLogged(
                trigger_id=event.id,
                ts=event.ts,
                detected_at=self._now(),
                question=event.question,
                matched_alias=event.matched_alias,
                score=event.score,
                needs_screen=event.needs_screen,
            )
        )
        if self.settings.autonomy is AutonomyLevel.L0:
            return
        try:
            self._trigger_q.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_triggers += 1
            log.warning("trigger %s dropped: one answer running and one pending", event.id)

    async def _answer_triggers(self) -> None:
        while (event := await self._trigger_q.get()) is not None:
            await self._handle(event)

    async def _handle(self, event: TriggerEvent) -> None:
        dispatched = time.perf_counter()
        # The overlay's "looking up context..." state runs alongside the brain call.
        notify = asyncio.create_task(
            self._guard("ui.notify_trigger", self._ui.notify_trigger(event))
        )
        request = BrainRequest(trigger=event, frame_paths=tuple(self._frame_paths(event)))
        reason: str | None = None
        try:
            response = await asyncio.wait_for(self._ask(request), self.settings.brain_timeout_s)
        except TimeoutError:
            reason = f"timeout: no answer after {self.settings.brain_timeout_s:g} s"
        except asyncio.CancelledError:
            notify.cancel()
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        log.debug("trigger %s: brain %.1f ms", event.id, (time.perf_counter() - dispatched) * 1e3)
        await notify
        if reason is not None:
            await self._fail(event, reason)
            return

        suggestion = Suggestion(
            id=f"s-{event.id}",
            trigger_id=event.id,
            text=response.text,
            excerpt=_excerpt(event),
            frames_sent=tuple(response.frames_sent),
        )
        try:
            await self._ui.show(suggestion)
        except Exception as exc:
            log.exception("ui.show failed for trigger %s", event.id)
            await self._fail(event, f"overlay: {type(exc).__name__}: {exc}")
            return
        shown_at = self._now()
        self._suggestions[suggestion.id] = suggestion.text
        self._store.log(
            SuggestionLogged(
                suggestion_id=suggestion.id,
                trigger_id=event.id,
                ts=shown_at,
                text=response.text,
                frames_sent=list(response.frames_sent),
                backend=response.backend,
                model=response.model,
                latency_ms=max(0, round((shown_at - event.ts).total_seconds() * 1000)),
                usage=response.usage,
            )
        )

    async def _ask(self, request: BrainRequest) -> BrainResponse:
        if self._brain_start is not None:
            # shield: a timeout here must not cancel the agent's startup for later triggers.
            await asyncio.shield(self._brain_start)
        return await self._brain.answer(request)

    async def _fail(self, event: TriggerEvent, reason: str) -> None:
        log.warning("trigger %s: %s", event.id, reason)
        self._store.log(AgentErrorLogged(trigger_id=event.id, ts=self._now(), error=reason))
        await self._guard("ui.notify_failure", self._ui.notify_failure(event, reason))

    def _frame_paths(self, event: TriggerEvent) -> list[Path]:
        if not event.needs_screen:
            return []
        paths: list[Path] = []
        for frame_id in event.candidate_frames:
            if len(paths) >= self.settings.max_frames:
                break
            path = self._resolve_frame(frame_id)
            if path is None:
                log.warning("trigger %s: frame %s not found", event.id, frame_id)
                continue
            paths.append(path)
        return paths

    def _default_frame_path(self, frame_id: str) -> Path | None:
        meeting_dir = self._store.meeting_dir
        if (record := self._frames.get(frame_id)) is not None:
            return meeting_dir / record.path
        path = meeting_dir / "frames" / f"{frame_id}.jpg"  # kept before this session started
        return path if path.is_file() else None

    # --- user actions and epochs -------------------------------------------------------

    async def _log_actions(self) -> None:
        async for action in self._ui.actions():
            final = action.text
            if final is None and action.kind is ActionKind.APPROVE:
                final = self._suggestions.get(action.suggestion_id)
            self._store.log(
                ActionLogged(
                    suggestion_id=action.suggestion_id,
                    ts=self._now(),
                    kind=action.kind,
                    final_text=final,
                )
            )

    async def _run_epochs(self) -> None:
        summarizer = self._summarizer
        if summarizer is None:
            return
        while True:
            await asyncio.sleep(self.settings.epoch_interval_s)
            epoch = self.epoch + 1
            summary = await summarizer(epoch)
            self.epoch = epoch
            if self._epoch_hook is not None:
                self._epoch_hook(epoch)
            self._store.log(
                SummaryEpochLogged(
                    ts=self._now(), epoch=epoch, model=summary.model, usage=summary.usage
                )
            )


def _excerpt(event: TriggerEvent, lines: int = 3) -> str:
    """The last few timeline lines up to the question, for the overlay."""
    tail = [e.to_line() for e in list(event.window)[-lines:]]
    return "\n".join(tail) if tail else event.question


async def _aclose(it: object) -> None:
    aclose = getattr(it, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception):
            await aclose()
