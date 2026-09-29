"""Agent providers (#47, epic #43): which CLI or key each backend needs, and their state.

Sombra launches the ``claude`` / ``codex`` CLIs the user already installed and logged in,
the way Paseo does. It never reads, copies or stores their subscription credentials: the
login state comes only from the CLIs' own status commands, verified on the real CLIs:

* ``claude auth status`` (JSON by default; ``loggedIn``, ``authMethod``, exit 0 when logged
  in, 1 when not; Claude Code CLI reference).
* ``codex login status`` ("Logged in using ChatGPT", "Logged in using an API key - …",
  "Not logged in"; exit 0 when logged in, 1 when not; ``codex-rs/cli/src/login.rs``).

Both run with the same small environment allowlist the backends use, with no API key,
so the answer is the login a meeting will actually use. Their raw output is never echoed:
``codex login status`` prints a masked key.

This module holds only the pure logic and the process seam; ``setup`` (the wizard) and
``doctor`` (the agent section) present it. Tests pass a fake :data:`Runner`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sombra.config.schema import AUTH_API_KEY, AUTH_SUBSCRIPTION, BrainConfig

# Same floors as brain.claude_code / brain.codex and summary.cli_models (ADR 0046, 0018):
# an older CLI is refused at run time, so setup and doctor refuse it first.
CLAUDE_MIN_VERSION = (2, 1, 285)
CODEX_MIN_VERSION = (0, 159, 1)
STATUS_TIMEOUT_S = 20.0

_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")


@dataclass(frozen=True, slots=True)
class CliSpec:
    """One agent CLI: how to find, install, update, check and log in to it."""

    name: str  # the executable
    label: str
    min_version: tuple[int, int, int]
    install: str  # official install command (a shell line), run only with consent
    update: str  # shell line that updates an old CLI
    docs: str
    status: tuple[str, ...]  # argv after the executable
    login: tuple[str, ...]
    env: tuple[str, ...]  # passed through to the status command (no API keys)
    login_hint: str  # what to run by hand


CLAUDE = CliSpec(
    name="claude",
    label="Claude Code",
    min_version=CLAUDE_MIN_VERSION,
    install="curl -fsSL https://claude.ai/install.sh | bash",
    update="claude update",
    docs="https://code.claude.com/docs/en/setup",
    status=("auth", "status", "--json"),
    login=("auth", "login"),
    env=(*_BASE_ENV, "CLAUDE_CONFIG_DIR"),
    login_hint="claude auth login",
)
CODEX = CliSpec(
    name="codex",
    label="Codex CLI",
    min_version=CODEX_MIN_VERSION,
    install="npm install -g @openai/codex",
    update="npm install -g @openai/codex@latest",
    docs="https://github.com/openai/codex",
    status=("login", "status"),
    login=("login",),
    env=(*_BASE_ENV, "CODEX_HOME"),
    login_hint="codex login  (or `codex login --device-auth` on a machine without a browser)",
)
CLIS = {"claude-code": CLAUDE, "codex": CODEX}

ANTHROPIC = "anthropic"  # keychain provider names (`sombra auth set <provider>`)
OPENAI = "openai"
KEY_PROVIDER = {"claude-api": ANTHROPIC, "codex": OPENAI}

BACKEND_LABEL = {
    "claude-code": "Claude Code CLI on your Claude Pro/Max subscription",
    "claude-api": "Claude over the Anthropic API, with a key from the keychain",
    "codex": "Codex CLI",
}


@dataclass(frozen=True, slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


# Runs argv to completion with the given environment; never a shell.
Runner = Callable[[Sequence[str], Mapping[str, str]], ProcResult]


def run_quiet(argv: Sequence[str], env: Mapping[str, str]) -> ProcResult:  # pragma: no cover
    """The real :data:`Runner`: no shell, no stdin, a timeout; a missing CLI is rc 127."""
    try:
        done = subprocess.run(  # noqa: S603 - argv is built here, never from a shell string
            list(argv),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return ProcResult(127, "", f"{argv[0]}: not found")
    except (OSError, subprocess.TimeoutExpired) as e:
        return ProcResult(126, "", f"{argv[0]}: {type(e).__name__}")
    return ProcResult(done.returncode, done.stdout, done.stderr)


def cli_env(spec: CliSpec, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The allowlisted environment: the CLI finds its own login, and no API key reaches it."""
    source = os.environ if environ is None else environ
    return {k: source[k] for k in spec.env if k in source}


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def version_str(version: tuple[int, ...]) -> str:
    return ".".join(map(str, version))


# --- state -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CliState:
    path: str | None
    version: tuple[int, int, int] | None = None

    @property
    def installed(self) -> bool:
        return self.path is not None

    def new_enough(self, spec: CliSpec) -> bool:
        return self.version is not None and self.version >= spec.min_version


def detect_cli(
    spec: CliSpec,
    which: Callable[[str], str | None],
    run: Runner,
    environ: Mapping[str, str] | None = None,
) -> CliState:
    path = which(spec.name)
    if path is None:
        return CliState(None)
    result = run([path, "--version"], cli_env(spec, environ))
    version = parse_version(result.stdout) if result.returncode == 0 else None
    return CliState(path, version)


class Login(StrEnum):
    SUBSCRIPTION = "subscription"  # a Claude / ChatGPT plan pays
    API_KEY = "api-key"  # the CLI is logged in with a key: the provider's API pays
    OTHER = "other"  # logged in some other way (cloud provider, workload identity)
    NONE = "none"
    UNKNOWN = "unknown"  # the status command failed or said something unexpected


def login_state(
    spec: CliSpec, path: str, run: Runner, environ: Mapping[str, str] | None = None
) -> Login:
    """What the CLI's own status command says. Its output is parsed, never echoed."""
    result = run([path, *spec.status], cli_env(spec, environ))
    if spec is CLAUDE:
        return _claude_login(result)
    return _codex_login(result)


def _claude_login(result: ProcResult) -> Login:
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return Login.NONE if result.returncode == 1 else Login.UNKNOWN
    if not data["loggedIn"]:
        return Login.NONE
    method = str(data.get("authMethod", "")).casefold()
    if data.get("apiKeySource") or "api" in method or "console" in method:
        return Login.API_KEY
    if data.get("apiProvider") not in (None, "firstParty"):
        return Login.OTHER  # Bedrock / Vertex / Foundry: not the subscription
    return Login.SUBSCRIPTION


def _codex_login(result: ProcResult) -> Login:
    text = f"{result.stdout}\n{result.stderr}".casefold()
    if "not logged in" in text:
        return Login.NONE
    if result.returncode == 0 and "logged in" in text:
        if "chatgpt" in text:
            return Login.SUBSCRIPTION
        if "api key" in text:
            return Login.API_KEY
        return Login.OTHER
    return Login.NONE if result.returncode == 1 else Login.UNKNOWN


# --- what a configuration needs ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Needs:
    """What one role (the agent, or summaries) needs from the machine."""

    role: str  # "agent" or "summaries"
    backend: str
    cli: CliSpec | None = None
    login: bool = False  # the CLI must be logged in
    key: str | None = None  # a keychain provider that must hold a key
    key_or_login: bool = False  # codex without `auth`: either one will do


def agent_needs(brain: BrainConfig) -> Needs:
    backend = brain.backend
    if backend == "claude-code":
        return Needs("agent", backend, cli=CLAUDE, login=True)
    if backend == "claude-api":
        return Needs("agent", backend, key=ANTHROPIC)
    uses_key = brain.uses_api_key
    if uses_key is None:
        return Needs("agent", backend, cli=CODEX, key=OPENAI, key_or_login=True)
    if uses_key:
        return Needs("agent", backend, cli=CODEX, key=OPENAI)
    return Needs("agent", backend, cli=CODEX, login=True)


def summary_needs(backend: str | None) -> Needs | None:
    """Summaries through a CLI use its own login only: the text models get no key (ADR 0046)."""
    if backend is None:
        return None
    if backend == "claude-api":
        return Needs("summaries", backend, key=ANTHROPIC)
    return Needs("summaries", backend, cli=CLIS[backend], login=True)


def describe(brain: BrainConfig) -> str:
    """One line naming the backend and who pays."""
    if brain.backend != "codex":
        return f"{brain.backend} ({BACKEND_LABEL[brain.backend]})"
    uses_key = brain.uses_api_key
    if uses_key is None:
        how = "an OpenAI key from the keychain if one is stored, else `codex login`"
    elif uses_key:
        how = "an OpenAI key from the keychain"
    else:
        how = "your ChatGPT plan through `codex login`"
    return f"codex (Codex CLI, paid by {how})"


def auth_for(backend: str, auth: str | None) -> str:
    """The auth mode setup writes: the backend's only one, else ``auth`` (default login)."""
    if backend == "claude-code":
        return AUTH_SUBSCRIPTION
    if backend == "claude-api":
        return AUTH_API_KEY
    return auth or AUTH_SUBSCRIPTION
