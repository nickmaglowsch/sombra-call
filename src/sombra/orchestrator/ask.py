"""Post-meeting Q&A: ``sombra ask <meeting|latest> "<question>" [--frames]``.

PRD use case "Depois da reunião" ("O que combinamos sobre o prazo?") and principle
"O agente busca, o orquestrador não empurra": this module finds the meeting folder,
starts the configured :class:`~sombra.contracts.Brain` read-only on it with the
post-meeting prompt variant (``brain.prompt.post_meeting_system_prompt``) and returns
the answer. The agent greps and reads ``transcript.md``, ``summary.md``, ``context/``
and, with ``--frames``, looks at screenshots by their ``TELA`` id.

The agent is the one ``[brain] backend`` names (#47): Claude over the API with the key
from the OS keychain, the Claude Code CLI on the user's subscription, or the Codex CLI.
Without ``--frames`` no screenshot may leave the machine. The API backend enforces
that with :class:`FrameGate`; the CLI backends read files themselves, so they run on
:class:`FramelessView`, a private copy of the folder without ``frames/``.

Every ``[HH:MM:SS]`` the answer cites is checked against ``transcript.md`` before it is
returned (:mod:`.citations`, #77): a time that starts no line is dropped, so it never
reaches stdout, and the question's tail gives the model the transcript's real clock range.

Nothing is written to the meeting folder, ``log.jsonl`` included. The existing event
types describe live triggers: logging a question asked hours later as a
``trigger``/``suggestion`` would count as a live answer in ``sombra report`` and
stretch the meeting's duration to the time of the question, skewing every per-hour
metric. Logging Q&A needs its own event type (a contract change, proposed on #20).
"""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from sombra.brain import prompt as _prompt
from sombra.brain.claude import (
    AnthropicClient,
    ClaudeBrain,
    ClaudeSettings,
    ModelClient,
    ModelReply,
    PromptKit,
)
from sombra.brain.claude_code import ClaudeCodeBrain, ClaudeCodeRunner, ClaudeCodeSettings
from sombra.brain.codex import CodexBrain, CodexRunner, CodexSettings
from sombra.config import BrainConfig, UserConfig
from sombra.contracts import AutonomyLevel, Brain, BrainRequest, BrainResponse, TriggerEvent
from sombra.orchestrator.citations import check_citations, clock_hint, transcript_times
from sombra.store import read_started_at, slugify
from sombra.store.meeting import MEETING_FILE, TRANSCRIPT_FILE

LATEST = "latest"
SUMMARY_FILE = "summary.md"
MAX_INLINE_SUMMARY_BYTES = 20_000  # bigger summaries stay on disk; the agent reads them
ASK_TIMEOUT_S = 120.0  # nobody is waiting in a call; grepping a meeting takes a few rounds

MODEL_ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5",
}
"""``[models] agent`` aliases from the user config -> API model ids."""

FRAME_TOOLS = frozenset({"view_frame"})
FRAMES_OFF_NOTE = "[imagem omitida: as telas só ficam disponíveis com --frames]"

Clock = Callable[[], datetime]


class MeetingNotFoundError(LookupError):
    """``<meeting>`` names no meeting folder."""


class ApiKeyMissingError(RuntimeError):
    """No API key available for the real backend."""


# --- meeting resolution -------------------------------------------------------------


def list_meetings(root: Path) -> list[Path]:
    """Meeting folders (those with a ``meeting.toml``) under ``root``, oldest first.

    Ordered by ``started_at``; folders whose ``meeting.toml`` is unreadable sort
    first, by name (the name starts with ``YYYY-MM-DD_HHMM`` anyway).
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and (p / MEETING_FILE).is_file()]

    def key(path: Path) -> tuple[float, str]:
        try:
            return (read_started_at(path).timestamp(), path.name)
        except (OSError, ValueError):
            return (float("-inf"), path.name)

    return sorted(found, key=key)


def resolve_meeting(ref: str, root: Path) -> Path:
    """Meeting folder for ``latest``, a path, a folder name, or a meeting name/slug.

    - ``latest``: the most recently started meeting under ``root``.
    - a path to a folder with a ``transcript.md`` (absolute, relative or ``~``).
    - a folder name under ``root`` (``2026-09-29_1430_daily-time-x``).
    - a meeting name or slug (``"Daily time X"``, ``daily-time-x``): the most recent
      meeting with that name, so a recurring meeting resolves to its last occurrence.
    """
    ref = ref.strip()
    if not ref:
        raise MeetingNotFoundError("empty meeting name")
    root = Path(root).expanduser()
    meetings = list_meetings(root)
    if ref == LATEST:
        if not meetings:
            raise MeetingNotFoundError(f"no meetings in {root}")
        return meetings[-1].resolve()

    as_path = Path(ref).expanduser()
    for candidate in (as_path, root / ref):
        if candidate.is_dir() and (candidate / TRANSCRIPT_FILE).is_file():
            return candidate.resolve()

    slug = slugify(ref)
    matches = [m for m in meetings if _slug_of(m.name) == slug or _name_of(m) == ref.casefold()]
    if not matches:
        raise MeetingNotFoundError(f"no meeting {ref!r} in {root} (try `latest` or a path)")
    return matches[-1].resolve()


def _slug_of(folder: str) -> str:
    """``2026-09-29_1430_daily-time-x-2`` -> ``daily-time-x`` (the ``-N`` dedupe suffix dropped)."""
    parts = folder.split("_", 2)
    slug = parts[2] if len(parts) == 3 else folder
    head, sep, tail = slug.rpartition("-")
    return head if sep and tail.isdigit() and head else slug


def _name_of(meeting: Path) -> str | None:
    try:
        with (meeting / MEETING_FILE).open("rb") as f:
            name = tomllib.load(f).get("name")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return name.strip().casefold() if isinstance(name, str) else None


# --- the question as a brain request -------------------------------------------------


def ask_trigger(question: str, *, ts: datetime, frames: bool) -> TriggerEvent:
    """A synthetic :class:`TriggerEvent` carrying the user's question to the brain."""
    return TriggerEvent(
        id=f"ask-{uuid.uuid4().hex[:12]}",
        ts=ts,
        question=question,
        matched_alias="",
        score=1.0,
        window=(),
        needs_screen=frames,
    )


def read_summary(meeting_dir: Path, max_bytes: int = MAX_INLINE_SUMMARY_BYTES) -> str | None:
    """``summary.md`` to inline in the question's tail, or None (absent, empty or too big)."""
    path = Path(meeting_dir) / SUMMARY_FILE
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() else None


def ask_prompt_kit(meeting_dir: Path, *, frames: bool) -> PromptKit:
    """``brain.prompt`` with the post-meeting system prompt and question tail swapped in.

    The tail ends with the transcript's real clock range (:func:`.citations.clock_hint`),
    so the model cites ``[14:30:05]`` and not an offset like ``[00:00:00]`` (#77).
    """
    hint = clock_hint(transcript_times(meeting_dir, day=datetime.now().astimezone()))
    build = partial(_prompt.build_ask_tail, summary_md=read_summary(meeting_dir))

    def build_tail(trigger: TriggerEvent, frame_paths: Sequence[Path] = ()) -> list[dict[str, Any]]:
        tail = build(trigger, frame_paths)
        return [*tail, {"type": "text", "text": hint}] if hint else tail

    return PromptKit(
        system_prompt=partial(_prompt.post_meeting_system_prompt, frames=frames),
        prefix_builder=_prompt.PrefixBuilder,
        build_tail=build_tail,
        render_request=_prompt.render_request,
        parse_frame_request=_prompt.parse_frame_request if frames else None,
    )


# --- frames gate --------------------------------------------------------------------


class FrameGate:
    """:class:`ModelClient` wrapper that keeps screenshots out of requests without ``--frames``.

    Removes the ``view_frame`` tool and replaces every image block (a ``read`` of
    ``frames/fNNNN.jpg`` also returns one) with a short note, so no image leaves the
    machine unless the user asked for frames.
    """

    def __init__(self, inner: ModelClient, *, frames: bool) -> None:
        self._inner = inner
        self.frames = frames

    async def create(self, request: dict[str, Any], *, timeout_s: float) -> ModelReply:
        if not self.frames:
            request = _without_frames(request)
        return await self._inner.create(request, timeout_s=timeout_s)

    async def close(self) -> None:
        await self._inner.close()


def _without_frames(request: dict[str, Any]) -> dict[str, Any]:
    out = dict(request)
    if "tools" in out:
        out["tools"] = [t for t in out["tools"] if t.get("name") not in FRAME_TOOLS]
    out["messages"] = [
        {**m, "content": _strip_images(m["content"])} if isinstance(m.get("content"), list) else m
        for m in out.get("messages", [])
    ]
    return out


def _strip_images(blocks: list[Any]) -> list[Any]:
    out: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(block)
        elif block.get("type") == "image":
            out.append({"type": "text", "text": FRAMES_OFF_NOTE})
        elif isinstance(block.get("content"), list):  # tool_result
            out.append({**block, "content": _strip_images(block["content"])})
        else:
            out.append(block)
    return out


# --- backend -------------------------------------------------------------------------


def resolve_model(name: str) -> str:
    """Config alias (``sonnet``) or full API model id -> API model id."""
    name = name.strip()
    return MODEL_ALIASES.get(name.casefold(), name)


def claude_brain(
    meeting_dir: Path,
    cfg: UserConfig,
    *,
    frames: bool,
    model: str | None = None,
    client: ModelClient | None = None,
    api_key: Callable[[], str] | None = None,
) -> ClaudeBrain:
    """A :class:`ClaudeBrain` set up for one post-meeting question.

    Longer deadline and more tool rounds than a live answer (nobody is waiting in a
    call, and grepping a whole meeting can take a few rounds); no cache warm-up and
    the 5-minute cache TTL, since it is a single question. ``api_key`` is the keychain
    lookup (``sombra auth set anthropic``); ``client`` replaces it in tests.
    """
    settings = ClaudeSettings(
        user_name=cfg.user.name or "o usuário",
        aliases=cfg.user.aliases,
        level=AutonomyLevel.L1,  # unused by the post-meeting prompt
        model=resolve_model(model or cfg.models.agent),
        max_tokens=2048,
        effort=None,
        timeout_s=ASK_TIMEOUT_S,
        max_tool_rounds=8,
        cache_ttl="5m",
        warm_on_start=False,
    )
    if client is None:
        if api_key is None:  # fail before starting anything when there is no key
            raise ApiKeyMissingError(
                "no anthropic API key: run `sombra auth set anthropic` (or `sombra setup`)"
            )
        client = AnthropicClient(api_key)
    return ClaudeBrain(
        settings, FrameGate(client, frames=frames), ask_prompt_kit(meeting_dir, frames=frames)
    )


def claude_code_brain(
    meeting_dir: Path,
    cfg: UserConfig,
    *,
    frames: bool,
    model: str | None = None,
    runner: ClaudeCodeRunner | None = None,
) -> ClaudeCodeBrain:
    """:class:`ClaudeCodeBrain` for one question, on the user's Claude subscription."""
    settings = ClaudeCodeSettings(
        user_name=cfg.user.name or "o usuário",
        aliases=cfg.user.aliases,
        level=AutonomyLevel.L1,  # unused by the post-meeting prompt
        model=model,
        effort=None,
        timeout_s=ASK_TIMEOUT_S,
        max_turns=10,
    )
    return ClaudeCodeBrain(settings, runner, ask_prompt_kit(meeting_dir, frames=frames))


def codex_brain(
    meeting_dir: Path,
    cfg: UserConfig,
    *,
    frames: bool,
    model: str | None = None,
    api_key: Callable[[], str] | None = None,
    runner: CodexRunner | None = None,
) -> CodexBrain:
    """:class:`CodexBrain` for one question; ``api_key=None`` uses ``codex login``."""
    settings = CodexSettings(
        user_name=cfg.user.name or "o usuário",
        aliases=cfg.user.aliases,
        level=AutonomyLevel.L1,
        model=model,
        reasoning_effort=None,
        timeout_s=ASK_TIMEOUT_S,
    )
    kit = ask_prompt_kit(meeting_dir, frames=frames)
    return CodexBrain(settings, runner, kit, api_key=api_key)


class FramelessView:
    """A CLI :class:`Brain` started on a private copy of the meeting without ``frames/``.

    ``sombra ask`` without ``--frames`` promises that no screenshot leaves the machine.
    The CLI agents read files with their own tools (Claude Code's ``Read`` returns
    images; Codex's shell can dump bytes), so the only sure way is not to give them the
    frames at all. The copy lives in a 0700 temp folder for one question and is removed
    by :meth:`close`; symlinks are copied as links, which the CLIs refuse to follow out.
    """

    def __init__(self, inner: Brain) -> None:
        self.inner = inner
        self.backend = getattr(inner, "backend", "unknown")
        self._tmp: tempfile.TemporaryDirectory[str] | None = None

    async def start(self, meeting_dir: Path) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sombra-ask-")
        view = Path(self._tmp.name) / Path(meeting_dir).name
        ignore = _top_level_frames(meeting_dir)
        await asyncio.to_thread(shutil.copytree, meeting_dir, view, symlinks=True, ignore=ignore)
        await self.inner.start(view)

    async def answer(self, request: BrainRequest) -> BrainResponse:
        return await self.inner.answer(request)

    async def close(self) -> None:
        try:
            await self.inner.close()
        finally:
            if self._tmp is not None:
                self._tmp.cleanup()
                self._tmp = None


def _top_level_frames(root: Path) -> Callable[[str, list[str]], set[str]]:
    top = str(Path(root))

    def ignore(directory: str, names: list[str]) -> set[str]:
        return {"frames"} & set(names) if str(Path(directory)) == top else set()

    return ignore


Keys = Callable[[str], Callable[[], str] | None]


def ask_brain(
    meeting_dir: Path,
    cfg: UserConfig,
    *,
    frames: bool,
    keys: Keys,
    model: str | None = None,
) -> Brain:
    """The configured backend (``[brain]``), set up for one post-meeting question.

    ``model`` (``--model``) overrides ``[models] agent``. Keys come from ``keys`` (the OS
    keychain); a CLI on its own login gets none. Raises :class:`ApiKeyMissingError` when
    the backend is paid with a key and none is stored.
    """
    from sombra.orchestrator.wiring import backend_model, key_provider

    brain_cfg: BrainConfig = cfg.brain
    backend = brain_cfg.backend
    name = backend_model(backend, model or cfg.models.agent)
    if backend == "claude-api":
        return claude_brain(
            meeting_dir, cfg, frames=frames, model=model, api_key=keys(key_provider(backend))
        )
    if backend == "claude-code":
        brain: Brain = claude_code_brain(meeting_dir, cfg, frames=frames, model=name)
    else:
        key = None
        if brain_cfg.uses_api_key is not False:
            key = keys(key_provider(backend))
            if key is None and brain_cfg.uses_api_key:
                raise ApiKeyMissingError(
                    "no openai API key: run `sombra auth set openai` (or `sombra setup`)"
                )
        brain = codex_brain(meeting_dir, cfg, frames=frames, model=name, api_key=key)
    return brain if frames else FramelessView(brain)


# --- running one question ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AskResult:
    meeting_dir: Path
    question: str
    response: BrainResponse
    elapsed_s: float
    dropped_citations: tuple[str, ...] = ()  # cited times not in transcript.md, removed


async def ask(
    brain: Brain,
    meeting_dir: Path,
    question: str,
    *,
    frames: bool,
    clock: Clock,
) -> AskResult:
    """Start ``brain`` on the meeting, ask one question, close it. Errors propagate.

    The answer comes back whole (the :class:`Brain` port does not stream), and its
    citations are checked here, before the caller prints anything.
    """
    question = question.strip()
    if not question:
        raise ValueError("the question is empty")
    started = time.perf_counter()
    try:
        await brain.start(meeting_dir)
        response = await brain.answer(
            BrainRequest(trigger=ask_trigger(question, ts=clock(), frames=frames))
        )
    finally:
        await brain.close()
    if not frames:
        # The gate stripped any image; do not report frames the model never saw.
        response = dataclasses.replace(response, frames_sent=[])
    checked = check_citations(response.text, set(transcript_times(meeting_dir, day=clock())))
    response = dataclasses.replace(response, text=checked.text)
    return AskResult(
        meeting_dir, question, response, time.perf_counter() - started, checked.dropped
    )
