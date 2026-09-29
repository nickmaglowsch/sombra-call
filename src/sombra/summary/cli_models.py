"""``TextModel`` adapters over the user's own logged-in agent CLIs (PRD C4, M3).

:class:`ClaudeCliTextModel` runs ``claude -p`` (Claude Pro/Max subscription) and
:class:`CodexCliTextModel` runs ``codex exec`` (ChatGPT plan, via ``codex login``), so
rolling summaries and minutes work with no API key. See
``docs/adr/0046-claude-code-backend.md``.

Plain text in, plain text out, and **no tools at all**: the CLI starts in an empty
private temp folder, with every tool, MCP server, hook, user setting and project file
off, and a run that reports a tool call anyway is an error. The system prompt (a
constant from ``summary.prompts``) goes in a private file (Claude) or as developer
instructions (Codex); the meeting content goes on stdin, never argv. The child gets a
minimal environment with no API key, so the CLI's own login pays; Sombra never touches
that login.

``summary`` must not import ``brain``, so the few lines of process handling here
mirror ``brain.claude_code`` and ``brain.codex`` on purpose.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sombra.contracts import Usage

CLAUDE_MIN_VERSION = (2, 1, 285)  # same floor as brain.claude_code (ADR 0046)
CODEX_MIN_VERSION = (0, 159, 1)  # same floor as brain.codex (ADR 0018)
CLAUDE_DEFAULT_NAME = "claude-code-default"
CODEX_DEFAULT_NAME = "codex-default"

_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
CLAUDE_ENV = (*_BASE_ENV, "CLAUDE_CONFIG_DIR")
CODEX_ENV = (*_BASE_ENV, "CODEX_HOME")
CLAUDE_FIXED_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}

# Codex features that add a tool; all switched off for a text-only run (codex 0.159.1).
# They are not enough: 0.159.1 still offers the model code-mode ``exec``/``wait``,
# ``request_user_input`` and the ``collaboration.*`` sub-agent tools, and no feature or
# config key removes them (ADR 0046). The allowlist below is the enforcement.
CODEX_TOOL_FEATURES = (
    "shell_tool",
    "unified_exec",
    "view_image",
    "multi_agent",
    "apps",
    "plugins",
    "browser_use",
    "computer_use",
    "image_generation",
    "in_app_browser",
    "tool_suggest",
    "skill_search",
    "sleep_tool",
    "hooks",
)
# Item types a text-only run may produce. Anything else (a command, a file change, a
# code-mode or sub-agent call, or a type a newer Codex adds) voids the output.
CODEX_INERT_ITEMS = frozenset({"agent_message", "reasoning", "error"})


class CliModelError(RuntimeError):
    """The CLI failed or misbehaved. ``summary`` callers log it and keep recording."""


class CliAuthError(CliModelError):
    """The CLI is not logged in; the message says how to fix it."""


class CliRateLimitError(CliModelError):
    """The subscription's usage window (or a rate limit) is exhausted."""


class CliTimeoutError(CliModelError):
    """The CLI did not finish in time; its whole process group was killed."""


@dataclass(frozen=True, slots=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], str, Mapping[str, str], Path, float], CliResult]


def run_process(
    argv: Sequence[str], stdin: str, env: Mapping[str, str], cwd: Path, timeout_s: float
) -> CliResult:
    """Run the CLI with no shell in its own process group; kill the group on timeout."""
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is built here, never from a shell string
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise CliModelError(f"CLI not found: {argv[0]!r} (install it or set the path)") from None
    except OSError as e:
        raise CliModelError(f"could not start {argv[0]!r}: {e.strerror}") from None
    try:
        out, err = proc.communicate(stdin.encode("utf-8"), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.communicate()
        raise CliTimeoutError(
            f"{Path(argv[0]).name} did not finish within {timeout_s:g} s"
        ) from None
    except BaseException:
        _kill_group(proc)
        proc.communicate()
        raise
    return CliResult(
        proc.returncode,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def minimal_env(names: Sequence[str], base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    return {k: source[k] for k in names if k in source}


def _jsonl(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _count(raw: Mapping[str, Any], name: str) -> int:
    value = raw.get(name)
    return max(0, int(value)) if isinstance(value, int | float) else 0


_AUTH_RE = re.compile(
    r"not logged in|/login|codex login|login expired|invalid api key|unauthori[sz]ed|"
    r"authentication|\b401\b",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"usage limit|rate limit|hit your limit|limit reached|\b429\b|quota", re.IGNORECASE
)


def _error(cli: str, message: str, fix: str, *, status: Any = None, kind: Any = None) -> Exception:
    detail = message.strip()[:300] or "no output"
    if status in (401, 403) or kind in ("authentication_failed", "oauth_org_not_allowed"):
        return CliAuthError(f"{cli} is not logged in ({detail}): {fix}")
    if status == 429 or kind == "rate_limit" or _RATE_RE.search(detail):
        return CliRateLimitError(f"{cli} usage limit or rate limit: {detail}")
    if _AUTH_RE.search(detail):
        return CliAuthError(f"{cli} is not logged in ({detail}): {fix}")
    return CliModelError(f"{cli} failed: {detail}")


class _CliTextModel:
    """Shared plumbing: version check once, a private temp folder per call."""

    cli = ""
    min_version: tuple[int, int, int] = (0, 0, 0)
    env_names: Sequence[str] = ()

    def __init__(self, executable: Sequence[str], timeout_s: float, runner: Runner | None) -> None:
        self._executable = tuple(executable)
        self._timeout_s = timeout_s
        self._run = runner if runner is not None else run_process
        self._checked = False

    def _env(self) -> dict[str, str]:
        return minimal_env(self.env_names)

    def _check_version(self, cwd: Path) -> None:
        if self._checked:
            return
        result = self._run([*self._executable, "--version"], "", self._env(), cwd, self._timeout_s)
        version = parse_version(result.stdout) if result.returncode == 0 else None
        if version is None or version < self.min_version:
            found = (result.stdout.strip() or result.stderr.strip() or "unknown")[:100]
            need = ".".join(map(str, self.min_version))
            raise CliModelError(f"{self.cli} CLI {need} or newer is required (found: {found})")
        self._checked = True


class ClaudeCliTextModel(_CliTextModel):
    """``TextModel`` over ``claude -p`` with no tools, paid by the user's subscription."""

    cli = "Claude Code"
    min_version = CLAUDE_MIN_VERSION
    env_names = CLAUDE_ENV
    fix = "run `claude` in a terminal and log in (/login) with your Claude subscription"

    def __init__(
        self,
        *,
        model: str | None = None,
        timeout_s: float = 180.0,
        executable: Sequence[str] = ("claude",),
        runner: Runner | None = None,
    ) -> None:
        super().__init__(executable, timeout_s, runner)
        self._model = model

    @property
    def name(self) -> str:
        return self._model or CLAUDE_DEFAULT_NAME

    def argv(self, system_prompt_file: Path) -> list[str]:
        argv = [*self._executable, "-p", "--output-format", "stream-json", "--verbose"]
        argv += [
            "--no-session-persistence",
            "--restricted",
            "--tools",
            "",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--setting-sources",
            "",
            "--settings",
            json.dumps({"disableAllHooks": True, "enableAllProjectMcpServers": False}),
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--max-turns",
            "1",
            "--system-prompt-file",
            str(system_prompt_file),
        ]
        if self._model is not None:
            argv += ["--model", self._model]
        return argv

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        with tempfile.TemporaryDirectory(prefix="sombra-summary-") as tmp:
            system_file = Path(tmp) / "system.md"
            system_file.write_text(system, encoding="utf-8")
            work = Path(tmp) / "work"  # empty: nothing for the model to see
            work.mkdir()
            self._check_version(work)
            env = {
                **self._env(),
                **CLAUDE_FIXED_ENV,
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens),
            }
            result = self._run(self.argv(system_file), user, env, work, self._timeout_s)
        return self._parse(result, max_tokens)

    def _parse(self, result: CliResult, max_tokens: int) -> tuple[str, Usage]:
        events = _jsonl(result.stdout)
        init = next((e for e in events if e.get("subtype") == "init"), None)
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if final is None:
            raise _error(self.cli, result.stderr or result.stdout, self.fix)
        if final.get("is_error") or final.get("subtype") != "success" or result.returncode:
            kinds = [e["error"] for e in events if e.get("type") == "assistant" and e.get("error")]
            message = final.get("result") or "; ".join(map(str, final.get("errors") or []))
            raise _error(
                self.cli,
                str(message or result.stderr),
                self.fix,
                status=final.get("api_error_status"),
                kind=kinds[-1] if kinds else None,
            )
        if (
            init is None
            or init.get("tools") != []
            or init.get("mcp_servers")
            or _foreign_plugins(init)
        ):
            raise CliModelError(f"{self.cli} did not start without tools; refusing its output")
        if any(_has_tool_use(e) for e in events):
            raise CliModelError(f"{self.cli} attempted a tool call in a text-only run")
        if final.get("stop_reason") == "max_tokens":
            raise CliModelError(f"{self.cli} output truncated at max_tokens={max_tokens}")
        raw = final.get("usage") or {}
        usage = Usage(
            input_tokens=_count(raw, "input_tokens"),
            output_tokens=_count(raw, "output_tokens"),
            cache_read_input_tokens=_count(raw, "cache_read_input_tokens"),
            cache_creation_input_tokens=_count(raw, "cache_creation_input_tokens"),
        )
        return str(final.get("result") or ""), usage


def _foreign_plugins(init: dict[str, Any]) -> bool:
    """True when the CLI loaded a plugin that isn't one of its own ``…@builtin`` ones."""
    return any(
        not (isinstance(p, dict) and str(p.get("source", "")).endswith("@builtin"))
        for p in init.get("plugins") or []
    )


def _has_tool_use(event: dict[str, Any]) -> bool:
    if event.get("type") != "assistant":
        return False
    content = (event.get("message") or {}).get("content") or []
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def _toml_str(value: str) -> str:
    """A TOML basic string (``-c key=value`` values are parsed as TOML)."""
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


class CodexCliTextModel(_CliTextModel):
    """``TextModel`` over ``codex exec`` with no tools, paid by the user's ``codex login``.

    Codex has no output-token cap flag, so ``max_tokens`` is not enforced here; the
    summary prompts already bound the length.
    """

    cli = "Codex"
    min_version = CODEX_MIN_VERSION
    env_names = CODEX_ENV
    fix = "run `codex login` in a terminal"

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str | None = "low",
        timeout_s: float = 180.0,
        executable: Sequence[str] = ("codex",),
        runner: Runner | None = None,
    ) -> None:
        super().__init__(executable, timeout_s, runner)
        self._model = model
        self._effort = reasoning_effort

    @property
    def name(self) -> str:
        return self._model or CODEX_DEFAULT_NAME

    def argv(self, work: Path, system: str) -> list[str]:
        argv = [*self._executable, "exec", "--json", "--color", "never"]
        argv += ["--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"]
        argv += ["--cd", str(work)]
        argv += [
            "-c",
            'default_permissions="sombra"',
            "-c",
            'permissions.sombra.filesystem={":minimal"="read"}',
            "-c",
            "permissions.sombra.network.enabled=false",
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "-c",
            "project_doc_max_bytes=0",
        ]
        for feature in CODEX_TOOL_FEATURES:
            argv += ["--disable", feature]
        argv += ["-c", f"developer_instructions={_toml_str(system)}"]
        if self._effort is not None:
            argv += ["-c", f"model_reasoning_effort={_toml_str(self._effort)}"]
        if self._model is not None:
            argv += ["--model", self._model]
        argv.append("-")
        return argv

    def complete(self, system: str, user: str, max_tokens: int) -> tuple[str, Usage]:
        with tempfile.TemporaryDirectory(prefix="sombra-summary-") as tmp:
            work = Path(tmp)
            self._check_version(work)
            result = self._run(self.argv(work, system), user, self._env(), work, self._timeout_s)
        return self._parse(result)

    def _parse(self, result: CliResult) -> tuple[str, Usage]:
        text = ""
        usage = Usage()
        completed = False
        errors: list[str] = []
        for event in _jsonl(result.stdout):
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text", ""))
            elif (
                kind in ("item.started", "item.updated", "item.completed")
                and item.get("type") not in CODEX_INERT_ITEMS
            ):
                raise CliModelError(
                    f"{self.cli} attempted a tool call in a text-only run ({item.get('type')})"
                )
            elif kind == "turn.completed":
                completed = True
                raw = event.get("usage") or {}
                cached = _count(raw, "cached_input_tokens")
                written = _count(raw, "cache_write_input_tokens")
                usage = Usage(
                    input_tokens=max(0, _count(raw, "input_tokens") - cached - written),
                    output_tokens=_count(raw, "output_tokens"),
                    cache_read_input_tokens=cached,
                    cache_creation_input_tokens=written,
                )
            elif kind == "turn.failed":
                errors.append(str((event.get("error") or {}).get("message", "turn failed")))
            elif kind == "error":
                errors.append(str(event.get("message", "error")))
        if not completed or result.returncode != 0:
            detail = errors[-1] if errors else result.stderr.strip() or "no output"
            raise _error(self.cli, detail, self.fix)
        return text.strip(), usage
