"""Post-meeting Q&A: ``sombra ask <meeting|latest> "<question>" [--frames]``.

PRD use case "Depois da reunião" ("O que combinamos sobre o prazo?") and principle
"O agente busca, o orquestrador não empurra": this module finds the meeting folder,
starts the configured :class:`~sombra.contracts.Brain` read-only on it with the
post-meeting prompt variant (``brain.prompt.post_meeting_system_prompt``) and returns
the answer. The agent greps and reads ``transcript.md``, ``summary.md``, ``context/``
and, with ``--frames``, looks at screenshots by their ``TELA`` id.

Nothing is written to the meeting folder, ``log.jsonl`` included. The existing event
types describe live triggers: logging a question asked hours later as a
``trigger``/``suggestion`` would count as a live answer in ``sombra report`` and
stretch the meeting's duration to the time of the question, skewing every per-hour
metric. Logging Q&A needs its own event type (a contract change, proposed on #20).
"""

from __future__ import annotations

import os
import time
import tomllib
import uuid
from collections.abc import Callable
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
from sombra.config import UserConfig
from sombra.contracts import AutonomyLevel, Brain, BrainRequest, BrainResponse, TriggerEvent
from sombra.store import read_started_at, slugify
from sombra.store.meeting import MEETING_FILE, TRANSCRIPT_FILE

LATEST = "latest"
SUMMARY_FILE = "summary.md"
MAX_INLINE_SUMMARY_BYTES = 20_000  # bigger summaries stay on disk; the agent reads them
API_KEY_ENV = "ANTHROPIC_API_KEY"

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
    """``brain.prompt`` with the post-meeting system prompt and question tail swapped in."""
    return PromptKit(
        system_prompt=partial(_prompt.post_meeting_system_prompt, frames=frames),
        prefix_builder=_prompt.PrefixBuilder,
        build_tail=partial(_prompt.build_ask_tail, summary_md=read_summary(meeting_dir)),
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


def env_api_key() -> str:
    """The Anthropic API key from ``ANTHROPIC_API_KEY``.

    Stopgap until ``sombra auth`` (privacy module) stores it in the OS keychain; the
    key is read from the process environment only, never from a file, and never logged.
    """
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise ApiKeyMissingError(f"set {API_KEY_ENV} to use `sombra ask`")
    return key


def claude_brain(
    meeting_dir: Path,
    cfg: UserConfig,
    *,
    frames: bool,
    model: str | None = None,
    client: ModelClient | None = None,
) -> ClaudeBrain:
    """A :class:`ClaudeBrain` set up for one post-meeting question.

    Longer deadline and more tool rounds than a live answer (nobody is waiting in a
    call, and grepping a whole meeting can take a few rounds); no cache warm-up and
    the 5-minute cache TTL, since it is a single question.
    """
    settings = ClaudeSettings(
        user_name=cfg.user.name or "o usuário",
        aliases=cfg.user.aliases,
        level=AutonomyLevel.L1,  # unused by the post-meeting prompt
        model=resolve_model(model or cfg.models.agent),
        max_tokens=2048,
        effort=None,
        timeout_s=120.0,
        max_tool_rounds=8,
        cache_ttl="5m",
        warm_on_start=False,
    )
    if client is None:
        key = env_api_key()  # fail before starting anything when there is no key
        client = AnthropicClient(lambda: key)
    return ClaudeBrain(
        settings, FrameGate(client, frames=frames), ask_prompt_kit(meeting_dir, frames=frames)
    )


# --- running one question ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AskResult:
    meeting_dir: Path
    question: str
    response: BrainResponse
    elapsed_s: float


async def ask(
    brain: Brain,
    meeting_dir: Path,
    question: str,
    *,
    frames: bool,
    clock: Clock,
) -> AskResult:
    """Start ``brain`` on the meeting, ask one question, close it. Errors propagate."""
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
        response = BrainResponse(
            text=response.text,
            frames_sent=[],
            backend=response.backend,
            model=response.model,
            usage=response.usage,
        )
    return AskResult(meeting_dir, question, response, time.perf_counter() - started)
