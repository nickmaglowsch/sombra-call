"""Codex backend: the same :class:`sombra.contracts.Brain` answered by the Codex CLI.

Implements PRD C5 (swap the agent with a config change) and the "Codex em sandbox
read-only" rule, with C2 (read-only agent) and C6 (images never enter history).
See ``docs/adr/0018-codex-backend.md``.

Every answer is one ``codex exec --json`` process:

* **Prompt** from ``brain.prompt`` (#8) through the same :class:`PromptKit` that
  :class:`~sombra.brain.claude.ClaudeBrain` uses. ``render_request`` builds the
  Messages-shaped request; its ``system`` becomes Codex *developer instructions*
  and its user content (prefix + tail, meeting content wrapped as data) goes on
  stdin. Image blocks are dropped from the text and the same frames are attached
  with ``--image``.
* **Sandbox.** The agent runs under a Codex permission profile that can *read*
  only the meeting folder (plus the OS minimum a shell needs), write nothing and
  reach no network. We don't use ``--sandbox read-only``, because it lets the
  agent read the whole disk (``~/.ssh`` included). A ``--sandbox`` flag would also
  switch Codex back to the legacy sandbox and silently drop the profile. User config, exec-policy
  rules, ``AGENTS.md``, web search and the image viewer are all off. Every flag
  comes from :func:`build_argv`, and nothing a caller passes can remove them.
* **No history.** ``--ephemeral`` writes no session file and every answer is a new
  process, so a frame reaches exactly one request (C6) and is never persisted
  outside ``frames/``.

The process is reached through :class:`CodexRunner` so tests can script a fake
Codex; :class:`SubprocessRunner` is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sombra.brain.claude import (
    BrainAPIError,
    BrainAuthError,
    BrainError,
    BrainRateLimitError,
    BrainTimeoutError,
    PrefixSource,
    PromptKit,
    default_prompt_kit,
)
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

BACKEND_NAME = "codex-cli"
DEFAULT_MODEL_LABEL = "codex-default"  # reported when the Codex CLI picks its own model
MAX_FRAMES = 3
PERMISSION_PROFILE = "sombra"
# Oldest CLI verified to honour every confinement flag (ADR 0018). An older CLI may
# ignore ``default_permissions`` and read the whole disk, so it is refused.
MIN_CLI_VERSION = (0, 159, 1)
API_KEY_ENV = "CODEX_API_KEY"  # read by ``codex exec`` (codex-rs/login)

# Environment variables passed through to the Codex process. Nothing else is
# inherited, so no other secret in the parent environment reaches the agent's shell.
PASSTHROUGH_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "CODEX_HOME")

# Item types that mean the agent tried to act outside a read-only shell in the
# folder. The sandbox blocks them; seeing one still voids the answer.
FORBIDDEN_ITEMS = frozenset({"file_change", "web_search", "mcp_tool_call", "collab_tool_call"})


class BrainSandboxError(BrainError):
    """The agent attempted a write, web search, MCP or sub-agent call."""

    kind = "sandbox_violation"


# --- settings ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CodexSettings:
    """Constructor settings (the orchestrator fills them from the user config)."""

    user_name: str
    aliases: Sequence[str] = ()
    allowed_topics: Sequence[str] = ()
    level: AutonomyLevel = AutonomyLevel.L2
    model: str | None = None  # None: the Codex CLI's default model
    reasoning_effort: str | None = "low"  # latency first; None = model default
    timeout_s: float = 12.0  # whole answer, process start and frame round included
    max_frame_rounds: int = 1  # extra runs to serve a ``PRECISO_DA_TELA fNNNN`` reply
    executable: Sequence[str] = ("codex",)


# --- the process seam ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class CodexRunner(Protocol):
    """Runs one Codex process to completion. Raises :class:`BrainError` subclasses."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> ProcessResult: ...


class SubprocessRunner:
    """:class:`CodexRunner` over :func:`asyncio.create_subprocess_exec` (no shell)."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> ProcessResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(env),
                cwd=cwd,
                start_new_session=True,  # own process group: a kill reaches sandbox helpers
            )
        except FileNotFoundError:
            raise BrainError(
                f"Codex CLI not found: {argv[0]!r} (install it or set the path)"
            ) from None
        except OSError as e:
            raise BrainError(f"could not start the Codex CLI: {e.strerror}") from None
        try:
            async with asyncio.timeout(timeout_s):
                out, err = await proc.communicate(stdin.encode("utf-8"))
        except TimeoutError:
            _kill_group(proc)
            await proc.wait()
            raise BrainTimeoutError(f"Codex did not answer within {timeout_s:g} s") from None
        except asyncio.CancelledError:
            _kill_group(proc)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(proc.wait())
            raise
        return ProcessResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the process and everything it started (shell commands, sandbox helpers)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def parse_version(text: str) -> tuple[int, int, int] | None:
    """``codex-cli 0.159.1`` -> ``(0, 159, 1)``; None when there is no version."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


# --- command line --------------------------------------------------------------------


def toml_str(value: str) -> str:
    """A TOML basic string literal (``-c key=value`` values are parsed as TOML)."""
    out = ['"']
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def sandbox_args(meeting_dir: Path) -> list[str]:
    """The confinement flags. Always part of :func:`build_argv`; see the module docstring."""
    folder = toml_str(str(meeting_dir))
    p = PERMISSION_PROFILE
    return [
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--cd",
        str(meeting_dir),
        "-c",
        f"default_permissions={toml_str(p)}",
        "-c",
        f'permissions.{p}.filesystem={{":minimal"="read",{folder}="read"}}',
        "-c",
        f"permissions.{p}.network.enabled=false",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        "features.view_image=false",
        "-c",
        "project_doc_max_bytes=0",
    ]


def build_argv(
    settings: CodexSettings,
    meeting_dir: Path,
    developer_instructions: str,
    frames: Sequence[Path],
) -> list[str]:
    """The full ``codex exec`` command line. The prompt itself goes on stdin (``-``)."""
    argv = [*settings.executable, "exec", "--json", "--color", "never"]
    argv += sandbox_args(meeting_dir)
    argv += ["-c", f"developer_instructions={toml_str(developer_instructions)}"]
    if settings.reasoning_effort is not None:
        argv += ["-c", f"model_reasoning_effort={toml_str(settings.reasoning_effort)}"]
    if settings.model is not None:
        argv += ["--model", settings.model]
    for frame in frames:
        argv += ["--image", str(frame)]
    argv.append("-")
    return argv


def process_env(api_key: str | None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A minimal environment: :data:`PASSTHROUGH_ENV` plus the API key, if any."""
    source = os.environ if base is None else base
    env = {k: source[k] for k in PASSTHROUGH_ENV if k in source}
    if api_key:
        env[API_KEY_ENV] = api_key
    return env


# --- prompt flattening ---------------------------------------------------------------


def flatten_request(request: dict[str, Any]) -> tuple[str, str]:
    """Split a ``render_request`` body into (developer instructions, stdin prompt).

    Text blocks are joined in order; image blocks are dropped (the frames go with
    ``--image``) and ``cache_control`` markers are ignored.
    """
    system = "\n\n".join(_texts(request.get("system", [])))
    parts: list[str] = []
    for message in request.get("messages", []):
        content = message.get("content", [])
        parts.extend(_texts(content) if isinstance(content, list) else [str(content)])
    return system, "\n\n".join(parts)


def _texts(blocks: Sequence[dict[str, Any]]) -> list[str]:
    return [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]


# --- JSONL events ----------------------------------------------------------------------


@dataclass(slots=True)
class CodexRun:
    """What one ``codex exec --json`` run reported."""

    text: str = ""
    usage: Usage = field(default_factory=Usage)
    commands: list[str] = field(default_factory=list)  # shell commands the agent ran
    outputs: list[str] = field(default_factory=list)  # and what they printed
    forbidden: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    completed: bool = False


def usage_from_codex(raw: Mapping[str, Any]) -> Usage:
    """Map a ``turn.completed`` usage to the contract's :class:`Usage`.

    Codex (like the OpenAI API) counts cached tokens inside ``input_tokens``; the
    contract follows Anthropic, where ``input_tokens`` is the uncached rest.
    Reasoning tokens are already part of ``output_tokens``.
    """

    def get(name: str) -> int:
        value = raw.get(name)
        return max(0, int(value)) if isinstance(value, int | float) else 0

    cached = get("cached_input_tokens")
    written = get("cache_write_input_tokens")
    return Usage(
        input_tokens=max(0, get("input_tokens") - cached - written),
        output_tokens=get("output_tokens"),
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=written,
    )


def parse_events(stdout: str) -> CodexRun:
    """Read the JSONL stream; unknown events and non-JSON lines are ignored."""
    run = CodexRun()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "turn.completed":
            run.completed = True
            run.usage = _add(run.usage, usage_from_codex(event.get("usage") or {}))
        elif kind == "turn.failed":
            run.errors.append(str((event.get("error") or {}).get("message", "turn failed")))
        elif kind == "error":
            run.errors.append(str(event.get("message", "error")))
        elif kind == "item.completed":
            _item(run, event.get("item") or {})
    return run


def _item(run: CodexRun, item: dict[str, Any]) -> None:
    kind = item.get("type")
    if kind == "agent_message":
        run.text = str(item.get("text", "")).strip()  # the last message is the answer
    elif kind == "command_execution":
        run.commands.append(str(item.get("command", "")))
        run.outputs.append(str(item.get("aggregated_output", "")))
    elif kind in FORBIDDEN_ITEMS:
        run.forbidden.append(item)


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_input_tokens=a.cache_read_input_tokens + b.cache_read_input_tokens,
        cache_creation_input_tokens=a.cache_creation_input_tokens + b.cache_creation_input_tokens,
    )


def _error_for(message: str) -> BrainError:
    low = message.lower()
    if "401" in low or "unauthorized" in low or "api key" in low or "403" in low:
        return BrainAuthError(f"Codex rejected the credentials: {message[:300]}")
    if "429" in low or "rate limit" in low:
        return BrainRateLimitError(f"Codex rate limit hit: {message[:300]}")
    return BrainAPIError(f"Codex failed: {message[:300]}")


# --- the brain ---------------------------------------------------------------------


class CodexBrain:
    """:class:`sombra.contracts.Brain` backed by the Codex CLI in a read-only sandbox."""

    backend = BACKEND_NAME

    def __init__(
        self,
        settings: CodexSettings,
        runner: CodexRunner | None = None,
        prompt: PromptKit | None = None,
        *,
        api_key: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self._runner = runner if runner is not None else SubprocessRunner()
        self._prompt = prompt if prompt is not None else default_prompt_kit()
        self._api_key = api_key  # None: use the Codex CLI's own login (``codex login``)
        self._tools: MeetingTools | None = None
        self._prefix: PrefixSource | None = None
        self._transcript_offset = 0
        self._lock = asyncio.Lock()
        self.last_run: CodexRun | None = None  # the last process's events, for audit

    @property
    def meeting_dir(self) -> Path:
        if self._tools is None:
            raise BrainError("brain not started")
        return self._tools.root

    async def start(self, meeting_dir: Path) -> None:
        """Check the CLI version, bind to the meeting folder, load the transcript. No API call."""
        s = self.settings
        tools = MeetingTools(meeting_dir)
        await self._check_version(tools.root)
        self._tools = tools
        system = self._prompt.system_prompt(s.user_name, s.aliases, s.allowed_topics, s.level)
        self._prefix = self._prompt.prefix_builder(self._tools.root, system)
        self._transcript_offset = 0
        self._sync_transcript()

    def start_epoch(self, summary_md: str) -> None:
        """New summary epoch (C4)."""
        self._require_prefix().start_epoch(summary_md)

    async def answer(self, request: BrainRequest) -> BrainResponse:
        async with self._lock:  # one answer at a time keeps the prefix append-only
            s = self.settings
            try:
                async with asyncio.timeout(s.timeout_s):
                    return await self._answer(request, time.monotonic() + s.timeout_s)
            except TimeoutError:
                raise BrainTimeoutError(f"no answer within {s.timeout_s:g} s") from None

    async def close(self) -> None:
        """Nothing to release: each answer's process has already exited."""

    # --- internals -------------------------------------------------------------------

    async def _check_version(self, cwd: Path) -> None:
        """Fail closed on a CLI too old for the permission profile (see MIN_CLI_VERSION)."""
        argv = [*self.settings.executable, "--version"]
        result = await self._runner.run(
            argv, stdin="", env=process_env(None), cwd=cwd, timeout_s=self.settings.timeout_s
        )
        version = parse_version(result.stdout) if result.returncode == 0 else None
        if version is None or version < MIN_CLI_VERSION:
            found = result.stdout.strip() or result.stderr.strip() or "unknown"
            need = ".".join(map(str, MIN_CLI_VERSION))
            raise BrainError(f"Codex CLI {need} or newer is required (found: {found[:100]})")

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
            return
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

    async def _answer(self, request: BrainRequest, deadline: float) -> BrainResponse:
        s = self.settings
        tools = self._tools
        if tools is None:
            raise BrainError("brain not started")
        frames = list(request.frame_paths)
        if len(frames) > MAX_FRAMES:
            raise BrainError(f"at most {MAX_FRAMES} frames per answer, got {len(frames)}")
        frames = [_check_frame(tools, f) for f in frames]

        self._sync_transcript(request.trigger)
        prefix = self._require_prefix().blocks()
        usage = Usage()
        for _round in range(s.max_frame_rounds + 1):
            run = await self._run(prefix, request.trigger, frames, deadline)
            usage = _add(usage, run.usage)
            frame_id = self._frame_request(run.text)
            if frame_id is None or _round == s.max_frame_rounds or len(frames) >= MAX_FRAMES:
                break
            try:
                extra = tools.frame_path(frame_id)
            except (ToolError, OSError) as e:
                log.info("model asked for frame %s: %s", frame_id, e)
                break
            if extra in frames:
                break
            frames.append(extra)  # one more run with that frame in the tail
        if not run.text:
            raise BrainError("Codex returned no answer")
        if self._frame_request(run.text) is not None:
            raise BrainError(f"Codex asked for a screen it could not get: {run.text}")
        return BrainResponse(
            text=run.text,
            frames_sent=[f.stem for f in frames],
            backend=BACKEND_NAME,
            model=s.model or DEFAULT_MODEL_LABEL,
            usage=usage,
        )

    async def _run(
        self,
        prefix: list[dict[str, Any]],
        trigger: TriggerEvent,
        frames: Sequence[Path],
        deadline: float,
    ) -> CodexRun:
        s = self.settings
        try:
            tail = self._prompt.build_tail(trigger, frames)
        except (ValueError, OSError) as e:
            raise BrainError(f"could not build the prompt tail: {e}") from None
        req = self._prompt.render_request(prefix, tail, model=s.model or "", max_tokens=0)
        developer, prompt = flatten_request(req)
        argv = build_argv(s, self.meeting_dir, developer, frames)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrainTimeoutError(f"no answer within {s.timeout_s:g} s")
        key = self._api_key() if self._api_key is not None else None
        result = await self._runner.run(
            argv, stdin=prompt, env=process_env(key), cwd=self.meeting_dir, timeout_s=remaining
        )
        run = parse_events(result.stdout)
        self.last_run = run
        if run.commands:
            log.info("codex ran %d read-only command(s) in the meeting folder", len(run.commands))
        if run.forbidden:
            kinds = sorted({str(i.get("type")) for i in run.forbidden})
            raise BrainSandboxError(f"agent attempted a forbidden action: {', '.join(kinds)}")
        if run.errors and not run.completed:
            raise _error_for(run.errors[-1])
        if result.returncode != 0:
            detail = run.errors[-1] if run.errors else result.stderr.strip() or "no output"
            raise _error_for(f"exit {result.returncode}: {detail}")
        return run

    def _frame_request(self, text: str) -> str | None:
        parse = self._prompt.parse_frame_request
        return parse(text) if parse is not None else None


def _check_frame(tools: MeetingTools, frame: Path) -> Path:
    real = frame.resolve()
    if not real.is_relative_to(tools.root / "frames") or not real.is_file():
        raise BrainError(f"frame outside the meeting's frames/ folder: {frame}")
    return real
