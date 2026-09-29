"""Builds the real modules from the user config and hands them to :class:`Session` (#17).

This is the one place where concrete packages meet. Every piece here is a thin
adapter between a module's constructor and a :class:`Session` port or hook:

* config values that need translating (the ``models.stt`` / ``models.agent`` aliases);
* the agent provider (#47): :func:`build_agent_brain` and :func:`build_summary_model` turn
  ``[brain]`` / ``[summary]`` into a ``Brain`` and a ``TextModel`` for every backend
  (``claude-code``, ``claude-api``, ``codex``), and :func:`agent_key` finds the key a
  backend needs in the keychain;
* :class:`SummaryHooks`: ``summary``'s epoch summarizer and minutes as the
  ``summarizer`` / ``epoch_hook`` / ``minutes`` hooks (C4, M3);
* :class:`PauseAdapter`: ``privacy.PauseController`` as a :class:`PauseState`;
* :func:`build_session`: the shared assembly used by ``sombra start`` (live capture,
  :mod:`.live`) and ``sombra replay`` (recorded files, :mod:`.replay`).

Modules keep reading nothing but their constructor arguments (ARCHITECTURE rule 3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sombra.brain.backend import create_brain
from sombra.brain.claude_code import ClaudeCodeBrain, ClaudeCodeSettings
from sombra.config import BrainConfig, UserConfig
from sombra.contracts import (
    ApprovalUI,
    AudioSource,
    AutonomyLevel,
    Brain,
    BrainRequest,
    BrainResponse,
    FrameMarker,
    FramePipeline,
    FrameRecord,
    LogEvent,
    ScreenSource,
    Suggestion,
    TimelineEntry,
    Transcriber,
    TriggerDetector,
    TriggerEvent,
    UserAction,
)
from sombra.orchestrator.ask import MODEL_ALIASES, resolve_model
from sombra.orchestrator.hooks import BlockedApp, EpochSummary, never_blocked
from sombra.orchestrator.session import Clock, Session, local_now
from sombra.orchestrator.settings import SessionSettings
from sombra.privacy import PauseController
from sombra.screen.pipeline import DedupeFramePipeline
from sombra.store import MeetingStore
from sombra.summary import (
    AnthropicTextModel,
    ClaudeCliTextModel,
    CodexCliTextModel,
    EpochSummarizer,
    TextModel,
    append_epoch,
    write_minutes,
)
from sombra.summary.epochs import DEFAULT_EPOCH_MINUTES
from sombra.summary.minutes import SUMMARY_FILE
from sombra.transcription import (
    SILERO_VAD,
    WHISPER_MODELS,
    TranscriptionSettings,
    WhisperTranscriber,
    default_models_dir,
    whisper_model,
)
from sombra.trigger import NameTriggerDetector

log = logging.getLogger(__name__)

ANTHROPIC = "anthropic"  # keychain provider name for the Claude key (`sombra auth set anthropic`)

OPENAI = "openai"  # keychain provider for the Codex backend (optional: `codex login` works too)


# --- config translation ----------------------------------------------------------------


def resolve_whisper_model(name: str) -> str:
    """``models.stt`` -> a key of ``WHISPER_MODELS``.

    The config default is ``"large-v3-turbo"`` while the model table is keyed by the
    quantised file (``"large-v3-turbo-q5_0"``), so a unique ``<name>-...`` match is
    accepted too.
    """
    if name in WHISPER_MODELS:
        return name
    matches = [key for key in WHISPER_MODELS if key.startswith(f"{name}-")]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(sorted(WHISPER_MODELS))
    raise ValueError(f"models.stt: unknown whisper model {name!r}; known: {known}")


def resolve_claude_model(name: str) -> str:
    """``models.agent`` / ``models.summary`` alias (``sonnet``) -> an API model id."""
    return resolve_model(name)


# Pre-#47 name of claude-api, still accepted by the functions below.
_LEGACY = {"claude": "claude-api"}
DEFAULT_ALIAS = "default"  # ``models.agent = "default"``: let the backend choose


def backend_model(backend: str, name: str) -> str | None:
    """A ``[models]`` alias for one backend; None keeps the backend's own default.

    * ``claude-api``: aliases become API model ids (``sonnet`` -> ``claude-sonnet-5-5``).
    * ``claude-code``: passed as is; the CLI resolves ``sonnet`` / ``opus`` / ``haiku`` /
      ``fable`` for your plan itself, and takes full ids too.
    * ``codex``: a Claude alias or id means nothing to Codex, so it gets its default.
    """
    backend = _LEGACY.get(backend, backend)
    name = name.strip()
    if not name or name.casefold() == DEFAULT_ALIAS:
        return None
    if backend == "claude-api":
        return resolve_model(name)
    if backend == "claude-code":
        return name
    if name.casefold() in MODEL_ALIASES or name.startswith("claude-"):
        return None
    return name


def agent_model(backend: str, name: str) -> str | None:
    """The model to pass the agent backend (see :func:`backend_model`)."""
    return backend_model(backend, name)


def key_provider(backend: str) -> str:
    """Keychain provider holding the key of ``brain.backend``."""
    return OPENAI if backend == "codex" else ANTHROPIC


Keys = Callable[[str], Callable[[], str] | None]
"""``provider`` -> a lazy getter for its keychain key, or None when none is stored."""


class MissingKeyError(ValueError):
    """The configured backend is paid with an API key and none is stored."""


def agent_key(brain: BrainConfig, keys: Keys) -> Callable[[], str] | None:
    """The key the agent backend gets, or None when it runs on a CLI login.

    ``claude-code`` never gets one (a key would move billing off the subscription);
    ``codex`` with ``auth = "subscription"`` neither, even when an OpenAI key is stored.
    Raises :class:`MissingKeyError` when the backend needs a key and none is stored.
    """
    uses_key = brain.uses_api_key
    if brain.backend == "claude-code" or uses_key is False:
        return None
    provider = key_provider(brain.backend)
    key = keys(provider)
    if key is None and uses_key:
        raise MissingKeyError(
            f"{brain.backend} needs the {provider} API key: run `sombra auth set {provider}` "
            "or `sombra setup`"
        )
    return key


def build_summary_model(
    backend: str | None, model: str, keys: Keys
) -> tuple[TextModel | None, str | None]:
    """``[summary]``'s resolved backend -> (text model, why there is none).

    CLI-backed models use the CLI's own login and never get a key (ADR 0046).
    """
    if backend is None:
        return None, '[summary] backend = "none": no rolling summaries or minutes'
    backend = _LEGACY.get(backend, backend)
    if backend == "claude-code":
        return ClaudeCliTextModel(model=backend_model(backend, model)), None
    if backend == "codex":
        return CodexCliTextModel(model=backend_model(backend, model)), None
    key = keys(ANTHROPIC)
    if key is None:
        return None, f"no {ANTHROPIC} key, so no summaries or minutes"
    return AnthropicTextModel(key, model=resolve_model(model)), None


def summary_model_for(cfg: UserConfig, keys: Keys) -> tuple[TextModel | None, str | None]:
    """The user config's summary model (``[summary] backend``, ``[models] summary``)."""
    return build_summary_model(cfg.summary.resolve(cfg.brain), cfg.models.summary, keys)


# --- hooks -----------------------------------------------------------------------------


class PauseAdapter:
    """``privacy.PauseController`` (``is_paused`` property) as a :class:`PauseState`."""

    def __init__(self, controller: PauseController) -> None:
        self.controller = controller

    def is_paused(self) -> bool:
        return self.controller.is_paused


class OneMarkerStore:
    """``TimelineStore`` over ``MeetingStore`` that writes each frame's ``TELA`` line once.

    TEMPORARY (#41): remove once ``TimelineStore.append_frame``'s marker contract is settled.

    ``MeetingStore.append_frame`` writes the index record *and* its marker, while
    ``Session`` also sends the same ``FrameMarker`` down the timeline queue (so the
    trigger detector sees it in order) and appends it again. This keeps the store's
    line and drops the echo. See issue #41.
    """

    def __init__(self, inner: MeetingStore) -> None:
        self.inner = inner
        self._marked: set[str] = set()

    @property
    def meeting_dir(self) -> Path:
        return self.inner.meeting_dir

    def append_entry(self, entry: TimelineEntry) -> None:
        if isinstance(entry, FrameMarker) and entry.frame_id in self._marked:
            self._marked.discard(entry.frame_id)
            return
        self.inner.append_entry(entry)

    def append_frame(self, record: FrameRecord) -> None:
        self.inner.append_frame(record)
        self._marked.add(record.id)

    def log(self, event: LogEvent) -> None:
        self.inner.log(event)

    def entries_since(self, since: datetime) -> list[TimelineEntry]:
        return self.inner.entries_since(since)


class NoTrigger:
    """``TriggerDetector`` for L0 without a configured name: record only, never fire."""

    def feed(self, entry: TimelineEntry) -> TriggerEvent | None:
        return None


class NoBrain:
    """``Brain`` for L0: the session never asks it anything (no API key needed)."""

    async def start(self, meeting_dir: Path) -> None:
        return None

    async def answer(self, request: BrainRequest) -> BrainResponse:
        raise RuntimeError("L0 records only; no brain is configured")

    async def close(self) -> None:
        return None


class SilentUI:
    """``ApprovalUI`` for L0: nothing is ever shown, so no overlay is opened."""

    async def notify_trigger(self, trigger: TriggerEvent) -> None:
        return None

    async def show(self, suggestion: Suggestion) -> None:
        return None

    async def notify_failure(self, trigger: TriggerEvent, reason: str) -> None:
        return None

    async def actions(self) -> AsyncIterator[UserAction]:
        never: asyncio.Future[UserAction] = asyncio.get_running_loop().create_future()
        yield await never


class SummaryHooks:
    """Rolling summary (C4) and end-of-meeting minutes (M3) as :class:`Session` hooks.

    The model calls are blocking, so they run in a worker thread. ``on_epoch`` gets the
    new summary text (``ClaudeBrain.start_epoch``: the one planned prefix change).
    """

    def __init__(
        self,
        store: MeetingStore,
        model: TextModel,
        *,
        started_at: datetime,
        on_epoch: Callable[[str], None] | None = None,
        clock: Clock = local_now,
    ) -> None:
        self._store = store
        self._model = model
        self._summarizer = EpochSummarizer(model, started_at=started_at)
        self._started_at = started_at
        self._since = started_at
        self._on_epoch = on_epoch
        self._now = clock

    @property
    def summary(self) -> str:
        return self._summarizer.summary

    async def summarize(self, epoch: int) -> EpochSummary:
        now = self._now()
        entries = self._store.entries_since(self._since)
        result = await asyncio.to_thread(self._summarizer.summarize, entries, now)
        self._since = now
        append_epoch(self._store.meeting_dir / SUMMARY_FILE, result.section)
        if result.event.epoch != epoch:
            log.warning("summary epoch %d written as %d", epoch, result.event.epoch)
        return EpochSummary(model=result.event.model, usage=result.event.usage)

    def epoch_started(self, epoch: int) -> None:
        if self._on_epoch is None:
            return
        try:
            self._on_epoch(self._summarizer.summary)
        except Exception:
            # e.g. the brain never started (no network at the start): the epoch itself
            # is written and logged; only the prompt prefix keeps the old summary.
            log.exception("epoch %d: the brain did not take the new summary", epoch)

    async def write_minutes(self) -> None:
        self._store.sync()
        result = await asyncio.to_thread(
            write_minutes, self._store.meeting_dir, self._model, day=self._started_at
        )
        log.info(
            "minutes written: %d action items, %d model calls",
            len(result.minutes.actions),
            result.calls,
        )


# --- builders --------------------------------------------------------------------------


def check_models(stt_model: str, models_dir: Path | None = None) -> str:
    """Resolve ``models.stt`` and check the model files are downloaded; return the key.

    The models load lazily inside the transcriber, where a missing file would only
    crash and restart the transcribe task, so callers check before capture starts.
    """
    model = resolve_whisper_model(stt_model)
    directory = models_dir or default_models_dir()
    for needed in (whisper_model(model), SILERO_VAD):
        if not (directory / needed.filename).is_file():
            raise FileNotFoundError(
                f"model {needed.filename} not found in {directory}; "
                f"run: uv run python scripts/download_models.py {model}"
            )
    return model


def build_transcriber(
    stt_model: str,
    *,
    vocabulary: Sequence[str] = (),
    models_dir: Path | None = None,
    n_threads: int = 4,
    use_gpu: bool = True,
) -> WhisperTranscriber:
    """Silero VAD + whisper.cpp; the user's names go in Whisper's prompt (T4)."""
    model = check_models(stt_model, models_dir)
    return WhisperTranscriber.from_settings(
        TranscriptionSettings(
            model=model,
            models_dir=models_dir or default_models_dir(),
            vocabulary=tuple(vocabulary),
            n_threads=n_threads,
            use_gpu=use_gpu,
        )
    )


def build_detector(name: str, aliases: Sequence[str]) -> TriggerDetector:
    names = [n for n in (name, *aliases) if n.strip()]
    if not names:
        return NoTrigger()
    return NameTriggerDetector(names[0], names[1:])


def build_agent_brain(
    backend: str,
    api_key: Callable[[], str] | None,
    *,
    user_name: str,
    aliases: Sequence[str],
    allowed_topics: Sequence[str],
    level: AutonomyLevel,
    model: str,
) -> Brain:
    """``brain.backend`` -> the live :class:`Brain` (C5).

    ``claude-api`` and ``codex`` go through ``brain.backend.create_brain``; ``claude-code``
    is :class:`ClaudeCodeBrain`, which never gets a key. ``api_key`` is what
    :func:`agent_key` returned.
    """
    backend = _LEGACY.get(backend, backend)
    if backend == "claude-code":
        settings = ClaudeCodeSettings(
            user_name,
            tuple(aliases),
            tuple(allowed_topics),
            level,
            model=backend_model(backend, model),
        )
        return ClaudeCodeBrain(settings)
    return create_brain(
        "claude" if backend == "claude-api" else backend,
        user_name=user_name,
        aliases=tuple(aliases),
        allowed_topics=tuple(allowed_topics),
        level=level,
        model=backend_model(backend, model),
        api_key=api_key,
    )


@dataclass
class Assembly:
    """A ready :class:`Session` plus what must be closed after it (overlay, store...)."""

    session: Session
    store: MeetingStore
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def close(self) -> None:
        for close in reversed(self.closers):
            try:
                await close()
            except Exception:
                log.exception("closing %r failed", close)
        self.store.close()


def build_session(
    meeting_dir: Path,
    *,
    audio: AudioSource,
    transcriber: Transcriber,
    screen: ScreenSource,
    detector: TriggerDetector,
    brain: Brain,
    ui: ApprovalUI,
    settings: SessionSettings,
    started_at: datetime,
    frames: FramePipeline | None = None,
    summary_model: TextModel | None = None,
    on_epoch: Callable[[str], None] | None = None,
    pause: PauseController | None = None,
    blocked: BlockedApp = never_blocked,
    clock: Clock = local_now,
) -> Assembly:
    """Wire one meeting. Without ``summary_model`` there are no epochs and no minutes."""
    store = MeetingStore(meeting_dir, started_at=started_at)
    hooks = (
        SummaryHooks(store, summary_model, started_at=started_at, on_epoch=on_epoch, clock=clock)
        if summary_model is not None
        else None
    )
    session = Session(
        audio=audio,
        transcriber=transcriber,
        screen=screen,
        frames=frames or DedupeFramePipeline(meeting_dir),
        store=OneMarkerStore(store),  # TEMPORARY (#41)
        detector=detector,
        brain=brain,
        ui=ui,
        settings=settings,
        summarizer=hooks.summarize if hooks else None,
        epoch_hook=hooks.epoch_started if hooks else None,
        minutes=hooks.write_minutes if hooks else None,
        pause=PauseAdapter(pause) if pause is not None else None,
        blocked=blocked,
        clock=clock,
    )
    return Assembly(session=session, store=store)


def epoch_interval_s() -> float:
    """The session's epoch cadence matches the summarizer's (25 min, C4)."""
    return DEFAULT_EPOCH_MINUTES * 60.0
