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
  switch Codex back to the legacy sandbox and silently drop the profile. On Linux the
  profile also reads the native ``codex`` binary file (:func:`codex_read_paths`):
  Codex re-runs itself inside bubblewrap for every shell command. User config, exec-policy
  rules, ``AGENTS.md``, web search and the image viewer are all off. Every flag
  comes from :func:`build_argv`, and nothing a caller passes can remove them.
* **Only the shell tool.** Codex's bundled model catalog turns on code mode (a
  JavaScript ``exec`` tool) and sub-agents per model, and no feature flag removes
  them. :meth:`CodexBrain.start` writes a copy of the catalog with those fields
  cleared (:func:`confine_catalog`) and every run loads it with
  ``model_catalog_json``, so the request offers only ``exec_command`` and
  ``write_stdin``.
* **Output tripwire.** Any JSONL event or item type outside
  :data:`ALLOWED_EVENTS` / :data:`ALLOWED_ITEMS` voids the answer. It is not the
  enforcement: on 0.159.1 a code-mode ``exec`` or ``spawn_agent`` call emits no item
  at all, which is why those tools are removed from the request instead.
* **Tool-call log (#74).** The JSONL also misses a shell command that Codex judges
  sandbox-denied (every Seatbelt denial on macOS). So each run also logs Codex's own
  ``codex_otel`` tool-call events to stderr (:data:`TELEMETRY_LOG_FILTER`), and
  :func:`reconcile_tool_calls` checks every call the model made against the items:
  a tool other than the shell, or a shell call nothing accounts for, voids the answer.
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
import shutil
import signal
import sys
import tempfile
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
# Newest CLI whose tool set was verified by request capture (ADR 0018, #57). A newer
# one may offer a tool the catalog and flags don't remove, so it is refused until
# ``tests/brain/test_brain_codex_capture.py`` passes on it and this is raised.
MAX_TESTED_CLI_VERSION = (0, 159, 1)
CAPTURE_TEST = "uv run pytest tests/brain/test_brain_codex_capture.py"
API_KEY_ENV = "CODEX_API_KEY"  # read by ``codex exec`` (codex-rs/login)

# Environment variables passed through to the Codex process. Nothing else is
# inherited, so no other secret in the parent environment reaches the agent's shell.
PASSTHROUGH_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "CODEX_HOME")

# JSONL event and item types proven inert (ADR 0018). Anything else, including a type a
# newer CLI adds, voids the answer: ``file_change``, ``web_search``, ``mcp_tool_call``,
# ``collab_tool_call``, ``todo_list`` and every unknown item fail closed. ``error`` items
# are warnings (e.g. unknown model metadata) and carry only a message. This is a
# tripwire for what Codex reports; calls it doesn't report (code-mode ``exec``,
# ``spawn_agent`` on 0.159.1) are stopped by removing the tools (CATALOG_OVERRIDES).
ALLOWED_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)
ALLOWED_ITEMS = frozenset({"agent_message", "reasoning", "command_execution", "error"})

# The tools the request offers (decision 7 of ADR 0018). A logged call to any other
# tool voids the answer, even though Codex answers it with "unsupported call".
SHELL_TOOLS = frozenset({"exec_command", "write_stdin"})
# ``RUST_LOG`` for every ``codex exec`` run (#74). It changes only what Codex prints on
# stderr: its default (``error`` and the OpenTelemetry exporters off) plus the
# ``codex_otel`` events at info. ``trace_safe`` names every tool call the model made
# (``codex.tool_call_received``) and each sandbox outcome; ``log_only`` has each call's
# arguments and the output the model got (``codex.tool_result``). Prompts show as
# ``[REDACTED]``. Pinned: the parent's ``RUST_LOG`` is never passed through.
TELEMETRY_LOG_FILTER = (
    "error,opentelemetry_sdk=off,opentelemetry_otlp=off,"
    "codex_otel.trace_safe=info,codex_otel.log_only=info"
)

# Model-catalog fields that add tools on codex-cli 0.159.1 and the value that removes
# them. ``tool_mode = "code_mode_only"`` swaps the shell for a JavaScript ``exec`` tool;
# ``multi_agent_version`` adds ``spawn_agent`` & co.; ``experimental_supported_tools``
# adds ``clock.sleep`` and async messaging; ``supports_search_tool`` adds ``tool_search``
# (which also hides deferred sub-agent tools); ``apply_patch_tool_type`` adds the
# ``apply_patch`` write tool. Verified by request capture (ADR 0018).
CATALOG_OVERRIDES: Mapping[str, Any] = {
    "tool_mode": None,
    "multi_agent_version": None,
    "experimental_supported_tools": [],
    "supports_search_tool": False,
    "apply_patch_tool_type": None,
}
CATALOG_FILE = "models.json"


class BrainSandboxError(BrainError):
    """Codex reported an event or item outside the proven-inert allowlist."""

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
    # The Codex CLI whose native binary the Linux sandbox may read (see
    # :func:`codex_read_paths`). None: ``executable[0]``. Set it when ``executable``
    # is a wrapper around the real CLI.
    codex_binary: str | None = None


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


def codex_read_paths(
    command: str,
    *,
    platform: str = sys.platform,
) -> tuple[Path, ...]:
    """Files the sandbox must read so Codex can start its own shell (#63).

    On Linux, Codex starts every shell command through bubblewrap, and inside it runs
    its own *native* binary again before the command. That binary must be readable in
    the sandbox, or every command fails with ``bwrap: execvp …/bin/codex: No such
    file or directory``: ``:minimal`` mounts only system roots, not an npm prefix
    (``/opt/…``, ``~/.npm-global``, nvm, pnpm) or wherever a release binary sits.

    ``command`` is resolved like the shell would (``shutil.which`` + ``realpath``).
    An npm install puts a Node shim on ``PATH`` (``bin/codex.js``); the native binary
    lives in the platform package (``@openai/codex-linux-<arch>/vendor/<triple>/bin``),
    nested in the ``codex`` package or next to it (hoisted, pnpm). Node runs only
    outside the sandbox, so it is not added. The result is the resolved native binary
    **file**, never its folder: whatever else sits next to it (a release binary in
    ``~/Downloads``, say) stays unreadable. macOS (Seatbelt) runs the command itself
    under ``sandbox-exec`` without re-running Codex, so there the result is empty.
    """
    if not platform.startswith("linux"):
        return ()
    found = shutil.which(command)
    if found is None:
        return ()
    real = Path(os.path.realpath(found))
    binaries = [real] if not _is_script(real) else _npm_native_binaries(real)
    paths: list[Path] = []
    for binary in binaries:
        path = Path(os.path.realpath(binary))  # a symlinked vendor binary: its target
        if path.is_file() and not _is_script(path) and path not in paths:
            paths.append(path)
    return tuple(paths)


def _is_script(path: Path) -> bool:
    """A ``#!`` script (the npm Node shim) rather than a native binary."""
    try:
        with path.open("rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def _npm_native_binaries(shim: Path) -> list[Path]:
    """The native binaries an npm shim (``<pkg>/bin/codex.js``) can start on Linux."""
    package = shim.parent.parent
    places = [package / "node_modules" / "@openai", package / "vendor"]
    if package.parent.name == "@openai":
        places.append(package.parent)  # hoisted, or pnpm's sibling symlink
    found: list[Path] = []
    for place in places:
        pattern = "*/bin/codex" if place.name == "vendor" else "codex-linux-*/vendor/*/bin/codex"
        found += sorted(p for p in place.glob(pattern) if "linux" in p.parent.parent.name)
    return found


def sandbox_args(
    meeting_dir: Path, model_catalog: Path, codex_files: Sequence[Path] = ()
) -> list[str]:
    """The confinement flags. Always part of :func:`build_argv`; see the module docstring.

    ``model_catalog`` is the file :func:`confine_catalog` produced; see ADR 0018 for the
    request capture that shows which tool each flag removes. ``codex_files`` are the
    extra read-only files from :func:`codex_read_paths`.
    """
    folder = toml_str(str(meeting_dir))
    extra = "".join(f',{toml_str(str(path))}="read"' for path in codex_files)
    p = PERMISSION_PROFILE
    return [
        # Never drop: no session file (C6), and on 0.159.1 it is also why a
        # ``spawn_agent`` call fails (the child finds no rollout). Pinned by a test.
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--cd",
        str(meeting_dir),
        "-c",
        f"default_permissions={toml_str(p)}",
        "-c",
        f'permissions.{p}.filesystem={{":minimal"="read",{folder}="read"{extra}}}',
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
        # Tools. The catalog removes code mode, sub-agents, sleep, tool_search and
        # apply_patch; the rest remove goals, multi-agent v1 and request_user_input.
        "-c",
        f"model_catalog_json={toml_str(str(model_catalog))}",
        "--disable",
        "goals",
        "--disable",
        "multi_agent",
        "-c",
        "tools.experimental_request_user_input={enabled=false}",
        # No effect on 0.159.1 (already off, or overridden by the catalog); they keep a
        # future default flip from bringing code mode or sub-agents back.
        "--disable",
        "code_mode",
        "--disable",
        "code_mode_only",
        "--disable",
        "multi_agent_v2",
    ]


def build_argv(
    settings: CodexSettings,
    meeting_dir: Path,
    developer_instructions: str,
    frames: Sequence[Path],
    model_catalog: Path,
    codex_files: Sequence[Path] = (),
) -> list[str]:
    """The full ``codex exec`` command line. The prompt itself goes on stdin (``-``)."""
    argv = [*settings.executable, "exec", "--json", "--color", "never"]
    argv += sandbox_args(meeting_dir, model_catalog, codex_files)
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


def confine_catalog(raw: str) -> dict[str, Any]:
    """Parse ``codex debug models --bundled`` and clear every tool-adding field.

    Raises :class:`BrainError` when the output isn't a catalog, so a CLI that changes
    the format stops the brain instead of running with its default tools.
    """
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise BrainError("Codex model catalog is not JSON") from None
    catalog: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise BrainError("Codex model catalog has no models")
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            raise BrainError("Codex model catalog has an unexpected entry")
        model.update(
            {k: list(v) if isinstance(v, list) else v for k, v in CATALOG_OVERRIDES.items()}
        )
    return catalog


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
    forbidden: list[str] = field(default_factory=list)  # event/item types off the allowlist
    warnings: list[str] = field(default_factory=list)  # ``error`` items (non-fatal)
    errors: list[str] = field(default_factory=list)
    completed: bool = False
    tool_calls: list[str] = field(default_factory=list)  # every tool the model called (log)
    command_items: int = 0  # distinct ``command_execution`` items in the JSONL
    _command_index: dict[str, int] = field(default_factory=dict, repr=False)


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
    """Read the JSONL stream. Non-JSON lines are ignored; event and item types off the
    allowlist are collected in :attr:`CodexRun.forbidden`."""
    run = CodexRun()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind not in ALLOWED_EVENTS:
            run.forbidden.append(f"event {kind!r}")
        elif kind in ("item.started", "item.updated"):
            if _check_item(run, event.get("item")):
                _item(run, event["item"], done=False)
        elif kind == "turn.completed":
            run.completed = True
            run.usage = _add(run.usage, usage_from_codex(event.get("usage") or {}))
        elif kind == "turn.failed":
            run.errors.append(str((event.get("error") or {}).get("message", "turn failed")))
        elif kind == "error":
            run.errors.append(str(event.get("message", "error")))
        elif kind == "item.completed" and _check_item(run, event.get("item")):
            _item(run, event["item"])
    return run


def _check_item(run: CodexRun, item: object) -> bool:
    """True when ``item`` is an allowed item; otherwise record it as forbidden."""
    kind = item.get("type") if isinstance(item, dict) else None
    if kind in ALLOWED_ITEMS:
        return True
    run.forbidden.append(f"item {kind!r}")
    return False


def _item(run: CodexRun, item: dict[str, Any], *, done: bool = True) -> None:
    kind = item.get("type")
    if kind == "command_execution":
        # One entry per item id: a command still running when Codex exits has only
        # ``item.started`` (#74), and it counts as much as a completed one.
        command = str(item.get("command", ""))
        output = str(item.get("aggregated_output") or "")
        key = item.get("id")
        index = run._command_index.get(key) if isinstance(key, str) else None
        if index is None:
            run.commands.append(command)
            run.outputs.append(output)
            run.command_items += 1
            if isinstance(key, str):
                run._command_index[key] = len(run.commands) - 1
        elif done or output:
            run.commands[index] = command
            run.outputs[index] = output
    elif not done:
        return
    elif kind == "agent_message":
        run.text = str(item.get("text", "")).strip()  # the last message is the answer
    elif kind == "error":
        run.warnings.append(str(item.get("message", "")))


# --- Codex's tool-call log (stderr) ----------------------------------------------------

# A tracing record starts a line: ``2026-09-30T18:50:53.614230Z  INFO spans: target: …``.
_LOG_RECORD = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+(?:TRACE|DEBUG|INFO|WARN|ERROR)\s", re.M
)
_LOG_FIELD = re.compile(r'([\w.]+)=(?:"((?:[^"\\]|\\.)*)"|(\S*))')
_TELEMETRY = "codex_otel."
_CALL_RECEIVED = 'codex_otel.trace_safe: event.name="codex.tool_call_received" '
_SANDBOX_OUTCOME = 'codex_otel.trace_safe: event.name="codex.sandbox_outcome" '
_TOOL_RESULT = 'codex_otel.log_only: event.name="codex.tool_result" '


@dataclass(slots=True)
class ToolLog:
    """What Codex's ``codex_otel`` events on stderr say about one run's tool calls."""

    present: bool = False  # at least one ``trace_safe`` record: the filter took effect
    calls: list[tuple[str, str]] = field(default_factory=list)  # (call_id, tool name)
    denied: set[str] = field(default_factory=set)  # call ids the sandbox denied
    # call id: (command, output), or None when its record is unreadable or not the only one
    results: dict[str, tuple[str, str] | None] = field(default_factory=dict)


def _log_records(stderr: str) -> list[str]:
    starts = [m.start() for m in _LOG_RECORD.finditer(stderr)]
    return [stderr[a:b] for a, b in zip(starts, [*starts[1:], len(stderr)], strict=False)]


def _fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _LOG_FIELD.finditer(text):
        value = re.sub(r"\\(.)", r"\1", m[2]) if m[2] is not None else m[3]
        out.setdefault(m[1], value)
    return out


def parse_tool_log(stderr: str) -> ToolLog:
    """Read Codex's ``codex_otel`` records (see :data:`TELEMETRY_LOG_FILTER`).

    The tool output inside a ``codex.tool_result`` record is text the command printed,
    so it could hold lines that look like records. Such a line can add a call, cut a
    result short or add a second result for a call, and each makes
    :func:`reconcile_tool_calls` fail closed.
    """
    log = ToolLog()
    for record in _log_records(stderr):
        head = record.split("\n", 1)[0]
        if "codex_otel.trace_safe: " in head:
            log.present = True
        if _CALL_RECEIVED in head:
            f = _fields(head.split(_CALL_RECEIVED, 1)[1])
            log.calls.append((f.get("call_id", ""), f.get("tool_name", "")))
        elif _SANDBOX_OUTCOME in head:
            f = _fields(head.split(_SANDBOX_OUTCOME, 1)[1])
            if f.get("outcome") == "denied":
                log.denied.add(f.get("call_id", ""))
        elif _TOOL_RESULT in head:
            _tool_result(log, record.split(_TOOL_RESULT, 1)[1])
    return log


def _tool_result(log: ToolLog, body: str) -> None:
    """``… call_id=s1 … arguments={"cmd": …} output=<text> mcp_server= …``: the command
    and the output the model got, with Codex's header (``Chunk ID`` … ``Output:``) cut."""
    head, sep, rest = body.partition(" arguments=")
    f = _fields(head)
    if f.get("tool_name") != "exec_command":
        return
    call_id = f.get("call_id", "")
    if call_id in log.results:  # a second record for one call: trust neither
        log.results[call_id] = None
        return
    log.results[call_id] = None
    try:
        arguments, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return
    output, sep, _ = rest[end:].removeprefix(" output=").rpartition(" mcp_server=")
    if not sep or not isinstance(arguments, dict) or not isinstance(arguments.get("cmd"), str):
        return
    _header, marker, printed = output.partition("\nOutput:\n")
    log.results[call_id] = (arguments["cmd"], printed if marker else output)


def reconcile_tool_calls(run: CodexRun, log: ToolLog) -> None:
    """Hold the JSONL to the tool calls Codex logged (#74), failing closed.

    Every shell call becomes a ``command_execution`` item, except one the sandbox
    denied: on 0.159.1 a command that exits within 150 ms with output such as
    ``Operation not permitted`` is refused before its item starts. That is every
    Seatbelt denial on macOS. Those calls are taken from the log. After that, a
    call to a tool that isn't offered, a shell call without an item, or an item
    without a logged call (the log didn't work) goes to :attr:`CodexRun.forbidden`.
    """
    if not log.present:
        if run.completed:
            run.forbidden.append("no codex tool-call log")
        return
    run.tool_calls = [name for _, name in log.calls]
    run.forbidden += [f"tool {name!r}" for _, name in log.calls if name not in SHELL_TOOLS]
    shell = [call_id for call_id, name in log.calls if name == "exec_command"]
    denied = [call_id for call_id in shell if call_id in log.denied]
    for call_id in denied:
        result = log.results.get(call_id)
        if result is None:
            run.forbidden.append("tool 'exec_command' without a logged result")
        else:
            run.commands.append(result[0])
            run.outputs.append(result[1])
    if run.command_items < len(shell) - len(denied):
        run.forbidden.append("tool 'exec_command' without an item")
    elif run.command_items > len(shell) - len(denied):
        run.forbidden.append("item 'command_execution' without a logged call")


def _stderr_detail(stderr: str) -> str:
    """stderr without the ``codex_otel`` records, for error messages.

    A record ends where the next one starts, so plain text Codex prints after one
    (``Error: …``) sits inside it. Only the record's own lines are dropped: its first
    line, or for a ``codex.tool_result`` everything through the ``mcp_server=`` line.
    """
    first = _LOG_RECORD.search(stderr)
    kept = [stderr[: first.start()] if first else stderr]
    for record in _log_records(stderr):
        head, _, rest = record.partition("\n")
        if _TELEMETRY not in head:
            kept.append(record)
        elif _TOOL_RESULT in head and " mcp_server=" in record:
            end = record.rindex(" mcp_server=")
            kept.append(record[end:].partition("\n")[2])
        else:
            kept.append(rest)
    return "".join(kept).strip()


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
        self._catalog_dir: Path | None = None
        self._codex_files: tuple[Path, ...] = ()
        self._lock = asyncio.Lock()
        self.last_run: CodexRun | None = None  # the last process's events, for audit

    @property
    def meeting_dir(self) -> Path:
        if self._tools is None:
            raise BrainError("brain not started")
        return self._tools.root

    @property
    def model_catalog(self) -> Path:
        """The tool-free model catalog every run loads (written by :meth:`start`)."""
        if self._catalog_dir is None:
            raise BrainError("brain not started")
        return self._catalog_dir / CATALOG_FILE

    async def start(self, meeting_dir: Path) -> None:
        """Check the CLI version, bind to the meeting folder, load the transcript. No API call."""
        s = self.settings
        tools = MeetingTools(meeting_dir)
        await self._check_version(tools.root)
        await self._write_catalog(tools.root)
        self._codex_files = codex_read_paths(s.codex_binary or s.executable[0])
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
        """Remove the model catalog; each answer's process has already exited."""
        self._drop_catalog()

    # --- internals -------------------------------------------------------------------

    async def _check_version(self, cwd: Path) -> None:
        """Fail closed on a CLI outside the verified range (see MIN/MAX_TESTED_CLI_VERSION)."""
        argv = [*self.settings.executable, "--version"]
        result = await self._runner.run(
            argv, stdin="", env=process_env(None), cwd=cwd, timeout_s=self.settings.timeout_s
        )
        version = parse_version(result.stdout) if result.returncode == 0 else None
        if version is None or version < MIN_CLI_VERSION:
            found = result.stdout.strip() or result.stderr.strip() or "unknown"
            need = ".".join(map(str, MIN_CLI_VERSION))
            raise BrainError(f"Codex CLI {need} or newer is required (found: {found[:100]})")
        if version > MAX_TESTED_CLI_VERSION:
            tested = ".".join(map(str, MAX_TESTED_CLI_VERSION))
            raise BrainError(
                f"Codex CLI {'.'.join(map(str, version))} is newer than {tested}, the last "
                "version whose tools were verified; it could give the agent tools Sombra "
                f"doesn't remove. Install it with `npm i -g @openai/codex@{tested}`, or "
                f"verify the new one with `{CAPTURE_TEST}` and raise MAX_TESTED_CLI_VERSION."
            )

    async def _write_catalog(self, cwd: Path) -> None:
        """Write the tool-free model catalog every run loads (see :data:`CATALOG_OVERRIDES`).

        It lives in a private temp folder, outside the meeting folder and so outside
        what the agent's sandbox can read.
        """
        argv = [*self.settings.executable, "debug", "models", "--bundled"]
        result = await self._runner.run(
            argv, stdin="", env=process_env(None), cwd=cwd, timeout_s=self.settings.timeout_s
        )
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip() or "no output")[:200]
            raise BrainError(f"could not read the Codex model catalog: {detail}")
        catalog = confine_catalog(result.stdout)
        self._drop_catalog()
        folder = Path(tempfile.mkdtemp(prefix="sombra-codex-"))  # 0700
        path = folder / CATALOG_FILE
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(catalog, f)
        self._catalog_dir = folder

    def _drop_catalog(self) -> None:
        if self._catalog_dir is not None:
            shutil.rmtree(self._catalog_dir, ignore_errors=True)
            self._catalog_dir = None

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
        argv = build_argv(
            s, self.meeting_dir, developer, frames, self.model_catalog, self._codex_files
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrainTimeoutError(f"no answer within {s.timeout_s:g} s")
        key = self._api_key() if self._api_key is not None else None
        env = {**process_env(key), "RUST_LOG": TELEMETRY_LOG_FILTER}
        result = await self._runner.run(
            argv, stdin=prompt, env=env, cwd=self.meeting_dir, timeout_s=remaining
        )
        run = parse_events(result.stdout)
        reconcile_tool_calls(run, parse_tool_log(result.stderr))
        self.last_run = run
        if run.commands:
            log.info("codex ran %d read-only command(s) in the meeting folder", len(run.commands))
        for warning in run.warnings:
            log.warning("codex: %s", warning[:300])
        if run.forbidden:
            kinds = ", ".join(sorted(set(run.forbidden)))
            raise BrainSandboxError(f"Codex reported an action outside the allowlist: {kinds}")
        if run.errors and not run.completed:
            raise _error_for(run.errors[-1])
        if result.returncode != 0:
            detail = run.errors[-1] if run.errors else _stderr_detail(result.stderr) or "no output"
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
