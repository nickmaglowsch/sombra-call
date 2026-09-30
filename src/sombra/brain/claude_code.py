"""Claude Code backend: the same :class:`sombra.contracts.Brain` answered by the user's own
logged-in ``claude`` CLI, so a Claude Pro/Max subscription pays for it.

Implements PRD C5 (swap the agent with a config change) with C2 (read-only agent) and
C6 (images never enter history). See ``docs/adr/0046-claude-code-backend.md``.

Every answer is one ``claude -p`` process, run headless:

* **Prompt** from ``brain.prompt`` (#8) through the same :class:`PromptKit` as
  :class:`~sombra.brain.claude.ClaudeBrain`. ``render_request``'s ``system`` becomes the
  CLI's system prompt (through a private temp file, never argv). The user content
  (prefix + tail, meeting content wrapped as data, 0-3 frames as base64 image blocks)
  goes on **stdin** as one ``stream-json`` user message.
* **Confinement.** Only ``Read``/``Grep``/``Glob`` exist, reads outside the meeting
  folder are refused three ways (``--restricted``, ``blockReadsOutsideWorkingDirectories``,
  ``dontAsk``), and every other tool, MCP server, hook, plugin setting, skill, the user's
  settings and ``CLAUDE.md`` are off. Every flag comes from :func:`build_argv`; nothing a
  caller passes can remove one. The CLI's ``init`` event is checked on every run, and a
  run that reports anything wider fails closed with :class:`BrainSandboxError`.
* **Subscription auth.** The child environment is a small allowlist. No
  ``ANTHROPIC_*`` variable ever reaches it: an API key would switch billing to the API.
  Sombra never reads, copies or stores the CLI's credentials.
* **No history.** ``--no-session-persistence`` and a new process per answer, so a frame
  reaches exactly one answer (C6) and nothing is written under ``~/.claude/projects``.

The process is reached through :class:`ClaudeCodeRunner` so tests can script a fake CLI;
:class:`SubprocessRunner` is the real one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from sombra.brain.claude import (
    BrainAPIError,
    BrainAuthError,
    BrainError,
    BrainOverloadedError,
    BrainRateLimitError,
    BrainTimeoutError,
    PrefixSource,
    PromptKit,
    default_prompt_kit,
)
from sombra.brain.codex import ProcessResult, parse_version
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

BACKEND_NAME = "claude-code-cli"
DEFAULT_MODEL_LABEL = "claude-code-default"  # reported when the CLI picks its own model
MAX_FRAMES = 3
# Oldest CLI verified to honour every confinement flag (ADR 0046). ``--restricted`` needs
# 2.1.248 and ``--permission-prompts`` 2.1.259; only 2.1.285 was checked end to end, so
# anything older is refused rather than trusted.
MIN_CLI_VERSION = (2, 1, 285)

ALLOWED_TOOLS = ("Read", "Grep", "Glob")
# Explicit deny rules on top of ``--tools``: if a future CLI adds one of these back to the
# tool list, the deny rule still refuses the call.
DENIED_TOOLS = (
    "Bash",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "Skill",
    "mcp__*",
)
# Well-known secret stores, denied by path as defense in depth (``--restricted`` already
# refuses everything outside the folder). A rule that would cover the meeting folder
# itself is dropped, since deny beats allow.
DENIED_READ_PATHS = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config",
    "~/.claude",
    "~/.claude.json",
    "~/.codex",
    "~/.netrc",
    "~/Library/Keychains",
)
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

# Environment passed through to the CLI. Nothing else is inherited: in particular no
# ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL, which would move billing
# off the subscription or redirect the traffic. HOME (and CLAUDE_CONFIG_DIR) let the CLI
# find its own login; Sombra never opens it.
PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "CLAUDE_CONFIG_DIR",
)
FIXED_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # no telemetry, error reports, updates
    "DISABLE_AUTOUPDATER": "1",
}

LOGIN_FIX = "run `claude` in a terminal and log in (/login) with your Claude subscription"


class BrainSandboxError(BrainError):
    """The CLI reported a wider tool set or permission mode than we asked for."""

    kind = "sandbox_violation"


# --- settings ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaudeCodeSettings:
    """Constructor settings (the orchestrator fills them from the user config)."""

    user_name: str
    aliases: Sequence[str] = ()
    allowed_topics: Sequence[str] = ()
    level: AutonomyLevel = AutonomyLevel.L2
    model: str | None = None  # None: the CLI's default model for the subscription
    effort: str | None = "low"  # latency first; None = the CLI's default
    timeout_s: float = 15.0  # whole answer: process start, tool rounds, frame round
    max_turns: int = 5  # model turns per process (tool rounds + the answer)
    max_frame_rounds: int = 1  # extra runs to serve a ``PRECISO_DA_TELA fNNNN`` reply
    executable: Sequence[str] = ("claude",)


# --- the process seam ----------------------------------------------------------------


class ClaudeCodeRunner(Protocol):
    """Runs one CLI process to completion. Raises :class:`BrainError` subclasses."""

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
    """:class:`ClaudeCodeRunner` over :func:`asyncio.create_subprocess_exec` (no shell)."""

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
                start_new_session=True,  # own process group: a kill reaches its children
            )
        except FileNotFoundError:
            raise BrainError(
                f"Claude Code CLI not found: {argv[0]!r} (install it or set the path)"
            ) from None
        except OSError as e:
            raise BrainError(f"could not start the Claude Code CLI: {e.strerror}") from None
        try:
            async with asyncio.timeout(timeout_s):
                out, err = await proc.communicate(stdin.encode("utf-8"))
        except TimeoutError:
            _kill_group(proc)
            await proc.wait()
            raise BrainTimeoutError(f"Claude Code did not answer within {timeout_s:g} s") from None
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
    """SIGKILL the CLI and everything it started (copied from ``brain.codex`` on purpose)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


# --- command line --------------------------------------------------------------------


def denied_read_rules(meeting_dir: Path, home: Path | None = None) -> list[str]:
    """``Read(...)`` deny rules for :data:`DENIED_READ_PATHS` that don't cover the folder."""
    home = Path.home() if home is None else home
    rules: list[str] = []
    for raw in DENIED_READ_PATHS:
        target = home / raw[2:]
        if meeting_dir == target or meeting_dir.is_relative_to(target):
            continue
        rules += [f"Read({raw})", f"Read({raw}/**)"]
    return rules


def cli_settings(meeting_dir: Path, home: Path | None = None) -> dict[str, Any]:
    """The ``--settings`` JSON: the only settings source the CLI loads besides managed ones."""
    return {
        "permissions": {
            "defaultMode": "dontAsk",
            "allow": [],
            "deny": [*DENIED_TOOLS, *denied_read_rules(meeting_dir, home)],
            "additionalDirectories": [],
            "disableBypassPermissionsMode": "disable",
            "blockReadsOutsideWorkingDirectories": True,
        },
        "disableAllHooks": True,
        "enableAllProjectMcpServers": False,
    }


def sandbox_args(meeting_dir: Path, home: Path | None = None) -> list[str]:
    """The confinement flags. Always part of :func:`build_argv`; see the module docstring."""
    settings = json.dumps(cli_settings(meeting_dir, home), separators=(",", ":"))
    return [
        "--no-session-persistence",
        "--restricted",
        "--tools",
        ",".join(ALLOWED_TOOLS),
        "--disallowedTools",
        ",".join(DENIED_TOOLS),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--setting-sources",
        "",
        "--settings",
        settings,
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP_CONFIG,
        "--disable-slash-commands",
    ]


def build_argv(
    settings: ClaudeCodeSettings,
    meeting_dir: Path,
    system_prompt_file: Path,
    *,
    home: Path | None = None,
) -> list[str]:
    """The full ``claude -p`` command line. The prompt itself goes on stdin (stream-json)."""
    argv = [*settings.executable, "-p"]
    argv += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    argv += sandbox_args(meeting_dir, home)
    argv += ["--max-turns", str(settings.max_turns)]
    argv += ["--system-prompt-file", str(system_prompt_file)]
    if settings.effort is not None:
        argv += ["--effort", settings.effort]
    if settings.model is not None:
        argv += ["--model", settings.model]
    return argv


def process_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A minimal environment: :data:`PASSTHROUGH_ENV` plus :data:`FIXED_ENV`. Never a key."""
    source = os.environ if base is None else base
    env = {k: source[k] for k in PASSTHROUGH_ENV if k in source}
    env.update(FIXED_ENV)
    return env


# --- prompt ----------------------------------------------------------------------------


def split_request(request: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Split a ``render_request`` body into (system prompt, user content blocks).

    Text and image blocks keep their order; ``cache_control`` markers are dropped: the
    CLI places its own four breakpoints and the API allows no more (ADR 0046).
    """
    system = "\n\n".join(
        str(b.get("text", "")) for b in request.get("system", []) if b.get("type") == "text"
    )
    content: list[dict[str, Any]] = []
    for message in request.get("messages", []):
        blocks = message.get("content", [])
        if not isinstance(blocks, list):
            blocks = [{"type": "text", "text": str(blocks)}]
        for block in blocks:
            if block.get("type") in ("text", "image"):
                content.append({k: v for k, v in block.items() if k != "cache_control"})
    return system, content


def stdin_message(content: list[dict[str, Any]]) -> str:
    """One ``--input-format stream-json`` user message line."""
    message = {"type": "user", "message": {"role": "user", "content": content}}
    return json.dumps(message, ensure_ascii=False) + "\n"


# --- stream-json events ----------------------------------------------------------------


@dataclass(slots=True)
class ClaudeCodeRun:
    """What one ``claude -p --output-format stream-json`` run reported."""

    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    text: str = ""
    model: str | None = None
    usage: Usage = field(default_factory=Usage)
    tool_uses: list[dict[str, Any]] = field(default_factory=list)  # name + input, for audit
    denials: list[dict[str, Any]] = field(default_factory=list)  # calls the CLI refused
    errors: list[str] = field(default_factory=list)  # assistant ``error`` kinds


def usage_from_cli(raw: Mapping[str, Any]) -> Usage:
    """Map the ``result`` usage (Anthropic semantics already) to the contract's :class:`Usage`."""

    def get(name: str) -> int:
        value = raw.get(name)
        return max(0, int(value)) if isinstance(value, int | float) else 0

    return Usage(
        input_tokens=get("input_tokens"),
        output_tokens=get("output_tokens"),
        cache_read_input_tokens=get("cache_read_input_tokens"),
        cache_creation_input_tokens=get("cache_creation_input_tokens"),
    )


def parse_stream(stdout: str) -> ClaudeCodeRun:
    """Read the stream-json output; unknown events and non-JSON lines are ignored."""
    run = ClaudeCodeRun()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            run.init = event
            if isinstance(event.get("model"), str):
                run.model = event["model"]
        elif kind == "assistant":
            _assistant(run, event)
        elif kind == "result":
            run.result = event
            run.usage = usage_from_cli(event.get("usage") or {})
            if isinstance(event.get("result"), str):
                run.text = event["result"].strip()
            denials = event.get("permission_denials")
            if isinstance(denials, list):
                run.denials = [d for d in denials if isinstance(d, dict)]
    return run


def _assistant(run: ClaudeCodeRun, event: dict[str, Any]) -> None:
    message = event.get("message") or {}
    model = message.get("model")
    if isinstance(model, str) and not model.startswith("<"):  # "<synthetic>" on errors
        run.model = model
    if event.get("error"):
        run.errors.append(str(event["error"]))
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            run.tool_uses.append({"name": block.get("name"), "input": block.get("input")})


_AUTH_RE = re.compile(
    r"not logged in|/login|log in|login expired|invalid api key|oauth|unauthori[sz]ed|"
    r"authentication",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"usage limit|rate limit|rate_limit|hit your limit|limit reached|limit will reset|"
    r"resets? (?:at|in)|\b429\b",
    re.IGNORECASE,
)


def error_for(message: str, status: int | None = None, kind: str | None = None) -> BrainError:
    """Classify a CLI failure. ``status`` is ``api_error_status``; ``kind`` the assistant error."""
    detail = message.strip()[:300] or "no output"
    if status in (401, 403) or kind in ("authentication_failed", "oauth_org_not_allowed"):
        return BrainAuthError(f"Claude Code is not logged in ({detail}): {LOGIN_FIX}")
    if status == 429 or kind == "rate_limit" or _RATE_RE.search(detail):
        return BrainRateLimitError(f"Claude subscription usage limit or rate limit: {detail}")
    if status in (503, 529) or kind == "overloaded" or "overloaded" in detail.lower():
        return BrainOverloadedError(f"Claude is overloaded: {detail}")
    if _AUTH_RE.search(detail):
        return BrainAuthError(f"Claude Code is not logged in ({detail}): {LOGIN_FIX}")
    return BrainAPIError(f"Claude Code failed: {detail}")


def check_confinement(run: ClaudeCodeRun) -> None:
    """Fail closed if the CLI reports a wider sandbox than :func:`build_argv` asked for."""
    init = run.init
    if init is None:
        raise BrainSandboxError("Claude Code sent no init event; cannot verify the sandbox")
    tools = init.get("tools")
    if not isinstance(tools, list) or not set(tools) <= set(ALLOWED_TOOLS):
        raise BrainSandboxError(f"Claude Code offered tools beyond Read/Grep/Glob: {tools}")
    if init.get("mcp_servers"):
        raise BrainSandboxError("Claude Code loaded MCP servers")
    plugins = init.get("plugins") or []
    foreign = sorted(
        str(p.get("source", p)) if isinstance(p, dict) else str(p)
        for p in plugins
        if not (isinstance(p, dict) and str(p.get("source", "")).endswith("@builtin"))
    )
    if foreign:
        raise BrainSandboxError(f"Claude Code loaded non-builtin plugins: {', '.join(foreign)}")
    if init.get("permissionMode") != "dontAsk":
        raise BrainSandboxError(
            f"Claude Code ran in permission mode {init.get('permissionMode')!r}, not dontAsk"
        )
    used = sorted({str(t["name"]) for t in run.tool_uses} - set(ALLOWED_TOOLS))
    if used:
        raise BrainSandboxError(f"agent called a forbidden tool: {', '.join(used)}")


# --- the brain ---------------------------------------------------------------------


class ClaudeCodeBrain:
    """:class:`sombra.contracts.Brain` backed by the user's logged-in Claude Code CLI."""

    backend = BACKEND_NAME

    def __init__(
        self,
        settings: ClaudeCodeSettings,
        runner: ClaudeCodeRunner | None = None,
        prompt: PromptKit | None = None,
    ) -> None:
        self.settings = settings
        self._runner = runner if runner is not None else SubprocessRunner()
        self._prompt = prompt if prompt is not None else default_prompt_kit()
        self._tools: MeetingTools | None = None
        self._prefix: PrefixSource | None = None
        self._transcript_offset = 0
        self._lock = asyncio.Lock()
        self.last_run: ClaudeCodeRun | None = None  # the last process's events, for audit

    @property
    def meeting_dir(self) -> Path:
        if self._tools is None:
            raise BrainError("brain not started")
        return self._tools.root

    async def start(self, meeting_dir: Path) -> None:
        """Check the CLI version, bind to the meeting folder, load the transcript. No model call."""
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
        """Fail closed on a CLI too old (or unknown) for the confinement flags."""
        argv = [*self.settings.executable, "--version"]
        result = await self._runner.run(
            argv, stdin="", env=process_env(), cwd=cwd, timeout_s=self.settings.timeout_s
        )
        version = parse_version(result.stdout) if result.returncode == 0 else None
        if version is None or version < MIN_CLI_VERSION:
            found = result.stdout.strip() or result.stderr.strip() or "unknown"
            need = ".".join(map(str, MIN_CLI_VERSION))
            raise BrainError(
                f"Claude Code CLI {need} or newer is required (found: {found[:100]}); "
                "update it with `claude update`"
            )

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
            raise BrainError("Claude Code returned no answer")
        if self._frame_request(run.text) is not None:
            raise BrainError(f"Claude Code asked for a screen it could not get: {run.text}")
        return BrainResponse(
            text=run.text,
            frames_sent=[f.stem for f in frames],
            backend=BACKEND_NAME,
            model=s.model or run.model or DEFAULT_MODEL_LABEL,
            usage=usage,
        )

    async def _run(
        self,
        prefix: list[dict[str, Any]],
        trigger: TriggerEvent,
        frames: Sequence[Path],
        deadline: float,
    ) -> ClaudeCodeRun:
        s = self.settings
        try:
            tail = self._prompt.build_tail(trigger, frames)
        except (ValueError, OSError) as e:
            raise BrainError(f"could not build the prompt tail: {e}") from None
        req = self._prompt.render_request(prefix, tail, model=s.model or "", max_tokens=0)
        system, content = split_request(req)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrainTimeoutError(f"no answer within {s.timeout_s:g} s")
        # The system prompt names the user and their topics: a private file, not argv.
        with tempfile.TemporaryDirectory(prefix="sombra-claude-") as tmp:
            system_file = Path(tmp) / "system.md"
            system_file.write_text(system, encoding="utf-8")
            argv = build_argv(s, self.meeting_dir, system_file)
            result = await self._runner.run(
                argv,
                stdin=stdin_message(content),
                env=process_env(),
                cwd=self.meeting_dir,
                timeout_s=remaining,
            )
        run = parse_stream(result.stdout)
        self.last_run = run
        return self._check(run, result)

    def _check(self, run: ClaudeCodeRun, result: ProcessResult) -> ClaudeCodeRun:
        res = run.result
        if res is None:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise error_for(detail)
        if res.get("is_error") or res.get("subtype") != "success" or result.returncode != 0:
            status = res.get("api_error_status")
            kind = run.errors[-1] if run.errors else None
            if res.get("subtype") == "error_max_turns":
                raise BrainError(f"no answer within {self.settings.max_turns} model turns")
            message = res.get("result") or "; ".join(map(str, res.get("errors") or []))
            raise error_for(
                str(message or result.stderr), status if isinstance(status, int) else None, kind
            )
        check_confinement(run)
        source = (run.init or {}).get("apiKeySource")
        if source not in (None, "none"):
            log.warning("Claude Code is billing through %s, not the subscription login", source)
        if run.tool_uses:
            log.info("claude code made %d read-only tool call(s)", len(run.tool_uses))
        if run.denials:
            names = sorted({str(d.get("tool_name")) for d in run.denials})
            log.warning("Claude Code refused %d call(s): %s", len(run.denials), ", ".join(names))
        return run

    def _frame_request(self, text: str) -> str | None:
        parse = self._prompt.parse_frame_request
        return parse(text) if parse is not None else None


def _check_frame(tools: MeetingTools, frame: Path) -> Path:
    real = frame.resolve()
    if not real.is_relative_to(tools.root / "frames") or not real.is_file():
        raise BrainError(f"frame outside the meeting's frames/ folder: {frame}")
    return real


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_input_tokens=a.cache_read_input_tokens + b.cache_read_input_tokens,
        cache_creation_input_tokens=a.cache_creation_input_tokens + b.cache_creation_input_tokens,
    )
