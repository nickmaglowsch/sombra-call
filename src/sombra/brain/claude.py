"""Claude backend: answers a trigger with a warm cached prefix and an ephemeral tail.

Implements :class:`sombra.contracts.Brain` with our own tool loop on the Messages
API (see ``docs/adr/0009-claude-backend-and-cache-ttl.md``). PRD C2, C6, C7 and the
C5 backend interface.

Every answer is one short, stateless conversation built from scratch:

* **prefix** (tools, system, context, epoch summary, transcript) from
  ``brain.prompt`` (#8); append-only, so it is byte-identical to the previous call
  up to the new transcript lines and hits the prompt cache;
* **tail** (last 60 s, the question, 0-3 frames) after the cache breakpoint.

Nothing from an answer is kept for the next one, so images never enter any later
request (C6). Tools are read-only and confined to the meeting folder (C2, see
``brain.tools``).

The model is reached through :class:`ModelClient`, the thin backend interface
(C5): :class:`AnthropicClient` is the real one; tests and a future Codex adapter
provide their own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from sombra.brain.tools import MeetingTools, ToolError
from sombra.contracts import (
    AutonomyLevel,
    BrainRequest,
    BrainResponse,
    TimelineEntry,
    TriggerEvent,
    Usage,
    parse_line,
)

log = logging.getLogger(__name__)

BACKEND_NAME = "claude-messages-api"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_FRAMES = 3

# --- errors ------------------------------------------------------------------------


class BrainError(Exception):
    """Any failure answering a trigger. The orchestrator logs it as ``AgentErrorLogged``."""

    kind = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BrainTimeoutError(BrainError):
    kind = "timeout"


class BrainOverloadedError(BrainError):
    """The API is overloaded (529/503); retried once before surfacing."""

    kind = "overloaded"


class BrainRateLimitError(BrainError):
    kind = "rate_limited"


class BrainAuthError(BrainError):
    kind = "auth"


class BrainRefusalError(BrainError):
    kind = "refusal"


class BrainAPIError(BrainError):
    kind = "api_error"


# --- the backend interface (C5) ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelReply:
    """One Messages API response, reduced to what the loop needs."""

    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage = field(default_factory=Usage)
    model: str | None = None  # the model that answered (differs after a refusal fallback)


class ModelClient(Protocol):
    """Sends one Messages-shaped request. Raises :class:`BrainError` subclasses."""

    async def create(self, request: dict[str, Any], *, timeout_s: float) -> ModelReply: ...

    async def close(self) -> None: ...


# --- prompt assembly seam (brain.prompt, #8) ---------------------------------------


class PrefixSource(Protocol):
    """What :class:`ClaudeBrain` needs from ``brain.prompt.PrefixBuilder``."""

    def add_transcript(self, entries: Sequence[TimelineEntry]) -> None: ...

    def start_epoch(self, summary_md: str) -> None: ...

    def blocks(self) -> list[dict[str, Any]]: ...


class RenderRequest(Protocol):
    def __call__(
        self,
        prefix: list[dict[str, Any]],
        tail: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PromptKit:
    """The prompt functions from ``brain.prompt``; injected so tests can fake them."""

    system_prompt: Callable[[str, Sequence[str], Sequence[str], AutonomyLevel], str]
    prefix_builder: Callable[[Path, str], PrefixSource]
    build_tail: Callable[[TriggerEvent, Sequence[Path]], list[dict[str, Any]]]
    render_request: RenderRequest
    # Returns the frame id when the model's whole answer is ``PRECISO_DA_TELA fNNNN``
    # (``brain.prompt.parse_frame_request``); None disables the check.
    parse_frame_request: Callable[[str], str | None] | None = None


# --- settings ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaudeSettings:
    """Constructor settings (the orchestrator fills them from the user config)."""

    user_name: str
    aliases: Sequence[str] = ()
    allowed_topics: Sequence[str] = ()
    level: AutonomyLevel = AutonomyLevel.L2
    model: str = DEFAULT_MODEL
    max_tokens: int = 1024
    effort: str | None = "low"  # latency first; None = model default
    timeout_s: float = 12.0  # whole answer, tool rounds included
    max_tool_rounds: int = 4
    cache_ttl: Literal["5m", "1h"] = "1h"  # ADR 0009
    warm_on_start: bool = True


# --- the brain ---------------------------------------------------------------------


class ClaudeBrain:
    """:class:`sombra.contracts.Brain` backed by Claude over the Messages API."""

    backend = BACKEND_NAME

    def __init__(self, settings: ClaudeSettings, client: ModelClient, prompt: PromptKit) -> None:
        self.settings = settings
        self._client = client
        self._prompt = prompt
        self._tools: MeetingTools | None = None
        self._prefix: PrefixSource | None = None
        self._transcript_offset = 0
        self._lock = asyncio.Lock()

    @property
    def meeting_dir(self) -> Path:
        if self._tools is None:
            raise BrainError("brain not started")
        return self._tools.root

    async def start(self, meeting_dir: Path) -> None:
        """Bind to the meeting folder, load the transcript so far and warm the cache."""
        s = self.settings
        self._tools = MeetingTools(meeting_dir)
        system = self._prompt.system_prompt(s.user_name, s.aliases, s.allowed_topics, s.level)
        self._prefix = self._prompt.prefix_builder(self._tools.root, system)
        self._transcript_offset = 0
        self._sync_transcript()
        if s.warm_on_start:
            await self._warm()

    def start_epoch(self, summary_md: str) -> None:
        """New summary epoch (C4): the one planned cache miss."""
        self._require_prefix().start_epoch(summary_md)

    async def answer(self, request: BrainRequest) -> BrainResponse:
        async with self._lock:  # one answer at a time keeps the prefix append-only
            s = self.settings
            deadline = time.monotonic() + s.timeout_s
            try:
                async with asyncio.timeout(s.timeout_s):
                    return await self._answer(request, deadline)
            except TimeoutError:
                raise BrainTimeoutError(f"no answer within {s.timeout_s:g} s") from None

    async def close(self) -> None:
        await self._client.close()

    # --- internals -------------------------------------------------------------------

    def _require_prefix(self) -> PrefixSource:
        if self._prefix is None:
            raise BrainError("brain not started")
        return self._prefix

    def _sync_transcript(self, trigger: TriggerEvent | None = None) -> None:
        """Feed complete new ``transcript.md`` lines to the prefix (append-only)."""
        prefix = self._require_prefix()
        path = self.meeting_dir / "transcript.md"
        if not path.is_file():
            return
        with path.open("rb") as f:
            f.seek(self._transcript_offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return  # no complete line yet
        self._transcript_offset += end + 1
        ref = trigger.ts if trigger is not None else datetime.now().astimezone()
        entries: list[TimelineEntry] = []
        for raw in chunk[: end + 1].decode("utf-8", errors="replace").splitlines():
            try:
                entries.append(parse_line(raw, day=ref))
            except ValueError:
                continue  # header block and blank lines
        if entries:
            prefix.add_transcript(entries)

    def _render(self, tail: list[dict[str, Any]], *, max_tokens: int) -> dict[str, Any]:
        s = self.settings
        req = self._prompt.render_request(
            self._require_prefix().blocks(), tail, model=s.model, max_tokens=max_tokens
        )
        req = dict(req)
        req["tools"] = MeetingTools.specs()
        if s.effort is not None:
            req["output_config"] = {**req.get("output_config", {}), "effort": s.effort}
        if s.cache_ttl != "5m":
            _set_cache_ttl(req, s.cache_ttl)
        return req

    async def _warm(self) -> None:
        """Pre-write the prefix to the cache with a ``max_tokens: 0`` request. Best effort."""
        req = self._render([{"type": "text", "text": "warmup"}], max_tokens=0)
        try:
            reply = await self._client.create(req, timeout_s=self.settings.timeout_s)
        except BrainError as e:
            log.warning("cache warm-up failed (%s): %s", e.kind, e.message)
            return
        log.info(
            "cache warm-up: wrote %d, read %d tokens",
            reply.usage.cache_creation_input_tokens,
            reply.usage.cache_read_input_tokens,
        )

    async def _answer(self, request: BrainRequest, deadline: float) -> BrainResponse:
        s = self.settings
        tools = self._tools
        if tools is None:
            raise BrainError("brain not started")
        frames = list(request.frame_paths)
        if len(frames) > MAX_FRAMES:
            raise BrainError(f"at most {MAX_FRAMES} frames per answer, got {len(frames)}")
        for frame in frames:
            _check_frame(tools, frame)

        self._sync_transcript(request.trigger)
        tail = self._prompt.build_tail(request.trigger, frames)
        req = self._render(tail, max_tokens=s.max_tokens)
        messages: list[dict[str, Any]] = list(req["messages"])

        usage = Usage()
        sent = [f.stem for f in frames]
        for _round in range(s.max_tool_rounds + 1):
            reply = await self._create({**req, "messages": messages}, deadline)
            usage = _add(usage, reply.usage)
            if reply.stop_reason == "refusal":
                raise BrainRefusalError("the model declined to answer")
            tool_uses = [b for b in reply.content if b.get("type") == "tool_use"]
            if reply.stop_reason == "tool_use" and tool_uses:
                # Off the event loop: file walks must never stall capture or the deadline.
                results = await asyncio.to_thread(_run_tools, tools, tool_uses, sent)
            else:
                text = _text(reply.content)
                frame_id = self._frame_request(text)
                if frame_id is None:
                    return BrainResponse(
                        text=text,
                        frames_sent=sent,
                        backend=BACKEND_NAME,
                        model=reply.model or s.model,
                        usage=usage,
                    )
                results = await asyncio.to_thread(_serve_frame, tools, frame_id, sent)
            messages = [
                *messages,
                {"role": "assistant", "content": reply.content},
                {"role": "user", "content": results},
            ]
        raise BrainError(f"no answer after {s.max_tool_rounds} tool rounds")

    def _frame_request(self, text: str) -> str | None:
        parse = self._prompt.parse_frame_request
        return parse(text) if parse is not None else None

    async def _create(self, req: dict[str, Any], deadline: float) -> ModelReply:
        """One request, retried once on overload within the answer's deadline."""
        for attempt in (1, 2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrainTimeoutError(f"no answer within {self.settings.timeout_s:g} s")
            try:
                return await self._client.create(req, timeout_s=remaining)
            except BrainOverloadedError:
                if attempt == 2:
                    raise
                log.warning("API overloaded; retrying once")
                await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic()) / 4))
        raise AssertionError("unreachable")  # pragma: no cover


def _check_frame(tools: MeetingTools, frame: Path) -> None:
    real = frame.resolve()
    if not real.is_relative_to(tools.root / "frames") or not real.is_file():
        raise BrainError(f"frame outside the meeting's frames/ folder: {frame}")


def _run_tools(
    tools: MeetingTools, blocks: list[dict[str, Any]], sent: list[str]
) -> list[dict[str, Any]]:
    """Run one round of tool calls (in a worker thread); record frames the model viewed."""
    results = []
    for block in blocks:
        result = _run_tool(tools, block)
        content = result["content"]
        if not result.get("is_error") and any(i.get("type") == "image" for i in content):
            for item in content:
                frame_id = _viewed_frame(item)
                if frame_id and frame_id not in sent:
                    sent.append(frame_id)
        results.append(result)
    return results


def _viewed_frame(item: dict[str, Any]) -> str | None:
    text = str(item.get("text", ""))
    if item.get("type") == "text" and text.startswith("TELA ") and text.endswith(":"):
        return text[5:-1]
    return None


def _serve_frame(tools: MeetingTools, frame_id: str, sent: list[str]) -> list[dict[str, Any]]:
    """Answer a ``PRECISO_DA_TELA fNNNN`` reply with the frame, served only from ``frames/``."""
    try:
        blocks = tools.view_frame(frame_id)
    except (ToolError, OSError) as e:
        note = f"A tela {frame_id} não está disponível ({e}). Responda sem ela."
        return [{"type": "text", "text": note}]
    if frame_id not in sent:
        sent.append(frame_id)
    return [*blocks, {"type": "text", "text": f"Aqui está a tela {frame_id}. Responda à pergunta."}]


def _run_tool(tools: MeetingTools, block: dict[str, Any]) -> dict[str, Any]:
    name = str(block.get("name", ""))
    try:
        content = tools.run(name, block.get("input") or {})
    except ToolError as e:
        log.info("tool %s refused: %s", name, e)
        return {
            "type": "tool_result",
            "tool_use_id": block["id"],
            "content": [{"type": "text", "text": str(e)}],
            "is_error": True,
        }
    except OSError as e:
        return {
            "type": "tool_result",
            "tool_use_id": block["id"],
            "content": [{"type": "text", "text": f"could not read: {e.strerror}"}],
            "is_error": True,
        }
    return {"type": "tool_result", "tool_use_id": block["id"], "content": content}


def _text(content: list[dict[str, Any]]) -> str:
    return "".join(str(b.get("text", "")) for b in content if b.get("type") == "text").strip()


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_input_tokens=a.cache_read_input_tokens + b.cache_read_input_tokens,
        cache_creation_input_tokens=a.cache_creation_input_tokens + b.cache_creation_input_tokens,
    )


def _set_cache_ttl(node: Any, ttl: str) -> None:
    """Set ``ttl`` on every ``cache_control`` marker in the request (in place)."""
    if isinstance(node, dict):
        cc = node.get("cache_control")
        if isinstance(cc, dict):
            node["cache_control"] = {**cc, "ttl": ttl}
        for key, value in node.items():
            if key != "cache_control":
                _set_cache_ttl(value, ttl)
    elif isinstance(node, list):
        for item in node:
            _set_cache_ttl(item, ttl)


def cache_hit_rate(usage: Usage) -> float:
    """Share of prompt tokens served from cache: read / (read + write + uncached)."""
    total = usage.cache_read_input_tokens + usage.cache_creation_input_tokens + usage.input_tokens
    return usage.cache_read_input_tokens / total if total else 0.0


def usage_from_api(raw: Any) -> Usage:
    """Map an Anthropic ``usage`` object (or dict) to the contract's :class:`Usage`."""

    def get(name: str) -> int:
        value = raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)
        return int(value or 0)

    return Usage(
        input_tokens=get("input_tokens"),
        output_tokens=get("output_tokens"),
        cache_read_input_tokens=get("cache_read_input_tokens"),
        cache_creation_input_tokens=get("cache_creation_input_tokens"),
    )


# --- the real client ---------------------------------------------------------------

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    """:class:`ModelClient` over the official ``anthropic`` SDK (imported lazily).

    The API key comes from an injected callable (the privacy module's keychain
    lookup) and is never logged. SDK retries are off: :class:`ClaudeBrain` owns the
    single retry on overload so it can respect the answer deadline.
    """

    def __init__(self, api_key: Callable[[], str], *, refusal_fallback: bool = True) -> None:
        self._api_key = api_key
        self._refusal_fallback = refusal_fallback
        self._sdk: Any = None

    def _client(self) -> Any:
        if self._sdk is None:
            import anthropic

            self._sdk = anthropic.AsyncAnthropic(api_key=self._api_key(), max_retries=0)
        return self._sdk

    async def create(self, request: dict[str, Any], *, timeout_s: float) -> ModelReply:
        import anthropic

        client = self._client().with_options(timeout=timeout_s)
        try:
            if self._refusal_fallback:
                message = await client.beta.messages.create(
                    **request, betas=[REFUSAL_FALLBACK_BETA], fallbacks="default"
                )
            else:
                message = await client.messages.create(**request)
        except anthropic.APITimeoutError:
            raise BrainTimeoutError("API request timed out") from None
        except (anthropic.OverloadedError, anthropic.ServiceUnavailableError) as e:
            raise BrainOverloadedError(f"API overloaded ({e.status_code})") from None
        except anthropic.RateLimitError:
            raise BrainRateLimitError("API rate limit hit") from None
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise BrainAuthError(f"API rejected the credentials ({e.status_code})") from None
        except anthropic.APIStatusError as e:
            raise BrainAPIError(f"API error {e.status_code}: {_safe(e.message)}") from None
        except anthropic.APIConnectionError:
            raise BrainAPIError("could not reach the API") from None
        data = message.to_dict()
        return ModelReply(
            content=list(data.get("content", [])),
            stop_reason=data.get("stop_reason"),
            usage=usage_from_api(data.get("usage") or {}),
            model=data.get("model"),
        )

    async def close(self) -> None:
        if self._sdk is not None:
            await self._sdk.close()
            self._sdk = None


def _safe(message: str) -> str:
    """API error text, trimmed; it never contains the key but may be long."""
    return message[:300]
