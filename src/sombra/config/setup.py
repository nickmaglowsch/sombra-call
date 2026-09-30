"""``sombra setup``: choose the agent provider, check it is ready, write it to the config (#47).

The Paseo model (epic #43): Sombra drives the ``claude`` / ``codex`` CLIs the user already
installed and logged in, or an API key kept in the OS keychain. The wizard:

1. detects the CLIs and their versions (``config.providers``);
2. shows the official install or update command for a missing or old CLI, and runs it
   only when the user says yes;
3. checks the login with the CLI's own status command, and offers to start the CLI's own
   login flow in this terminal. Sombra never sees, stores or forwards the token;
4. for API-key mode, delegates to ``sombra auth set <provider>`` (keychain only);
5. writes ``[brain]`` / ``[summary]`` to ``config.toml`` with ``tomlkit``, so every other
   key and every comment stays as it was;
6. asks one short question through the chosen backend (the same builder ``sombra ask``
   uses), so a broken login or flag shows up now and not in the middle of a meeting.

``--non-interactive`` never prompts, installs or logs in; ``--show`` only reports.
Everything that touches the machine goes through :class:`SetupIO`, so tests use fakes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

import tomlkit
from tomlkit.items import Table

from sombra.config.loader import default_config_path, load_user_config
from sombra.config.providers import (
    BACKEND_LABEL,
    CLAUDE,
    CODEX,
    CliSpec,
    CliState,
    Login,
    Needs,
    Runner,
    agent_needs,
    auth_for,
    describe,
    detect_cli,
    login_state,
    run_quiet,
    summary_needs,
    version_str,
)
from sombra.config.schema import (
    AUTH_API_KEY,
    AUTH_SUBSCRIPTION,
    SUMMARY_BACKENDS,
    SUMMARY_FOLLOW,
    BrainConfig,
    ConfigError,
    SummaryConfig,
    UserConfig,
)


class SetupError(ValueError):
    """Bad arguments; nothing was changed."""


# (label, backend, auth) in the order the wizard offers them.
CHOICES: tuple[tuple[str, str, str], ...] = (
    ("Claude, on your Claude Pro/Max plan (Claude Code CLI)", "claude-code", "subscription"),
    ("Claude, on an Anthropic API key (billed per token)", "claude-api", "api-key"),
    ("Codex, on your ChatGPT plan (Codex CLI, `codex login`)", "codex", "subscription"),
    ("Codex, on an OpenAI API key (Codex CLI, billed per token)", "codex", "api-key"),
)

SMOKE_QUESTION = "Responda só com a palavra: pronto"


@dataclass(frozen=True, slots=True)
class SetupRequest:
    config: Path
    backend: str | None = None
    auth: str | None = None
    summary: str | None = None
    interactive: bool = True
    show: bool = False
    smoke: bool = True

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> SetupRequest:
        path = args.config.expanduser() if args.config else default_config_path()
        if args.auth is not None and args.backend not in (None, "codex"):
            raise SetupError(f"--auth applies to codex only ({args.backend} has one mode)")
        if args.non_interactive and not args.show and args.backend is None:
            raise SetupError("--non-interactive needs --backend")
        return cls(
            config=path,
            backend=args.backend,
            auth=args.auth,
            summary=args.summary_backend,
            interactive=not args.non_interactive,
            show=args.show,
            smoke=not args.no_smoke,
        )


@dataclass
class SetupIO:
    """Everything the wizard does to the machine. Tests pass fakes."""

    out: TextIO
    ask: Callable[[str], str]  # input(); EOFError means "no"
    which: Callable[[str], str | None]
    run: Runner  # quiet: versions and status commands
    run_interactive: Callable[[Sequence[str]], int]  # the terminal is the CLI's (logins)
    has_key: Callable[[str], bool]  # raises when the keychain is unreachable
    set_key: Callable[[str], int]  # `sombra auth set <provider>`; returns its exit code
    smoke: Callable[[UserConfig], str]  # one answer through the configured agent
    environ: Mapping[str, str] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic


# --- config file -------------------------------------------------------------------------


def write_provider(path: Path, backend: str, auth: str | None, summary: str | None) -> None:
    """Set ``[brain] backend`` (+ ``auth`` for codex) and ``[summary] backend`` in place.

    Every other key, comment and blank line is kept (``tomlkit`` round-trips them). A
    missing file starts from the documented defaults (``sombra config init``). The file
    is replaced atomically and must load cleanly before and after.
    """
    from sombra.config.commands import DEFAULT_CONFIG_TOML

    if path.exists():
        load_user_config(path)  # refuse to rewrite a file that is already broken
        text = path.read_text(encoding="utf-8")
    else:
        text = DEFAULT_CONFIG_TOML
    doc = tomlkit.parse(text)
    brain = _table(doc, "brain")
    brain["backend"] = backend
    if backend == "codex" and auth is not None:
        brain["auth"] = auth
    elif "auth" in brain:
        del brain["auth"]
    if summary is not None and (summary != SUMMARY_FOLLOW or "summary" in doc):
        _table(doc, "summary")["backend"] = summary
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.sombra-setup")
    tmp.write_text(tomlkit.dumps(doc), encoding="utf-8")
    try:
        load_user_config(tmp)
    except ConfigError:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)


def _table(doc: tomlkit.TOMLDocument, name: str) -> Table:
    existing = doc.get(name)
    if isinstance(existing, Table):
        return existing
    table = tomlkit.table()
    doc[name] = table
    return table


# --- the wizard --------------------------------------------------------------------------


class _Wizard:
    def __init__(self, req: SetupRequest, io: SetupIO) -> None:
        self.req = req
        self.io = io
        self.problems: list[str] = []

    # small helpers
    def say(self, text: str = "") -> None:
        self.io.out.write(text + "\n")
        self.io.out.flush()

    def confirm(self, question: str, default: bool = False) -> bool:
        if not self.req.interactive:
            return False
        hint = "[Y/n]" if default else "[y/N]"
        try:
            answer = self.io.ask(f"{question} {hint} ").strip().casefold()
        except EOFError:
            return False
        if not answer:
            return default
        return answer in ("y", "yes", "s", "sim")

    def pick(self, question: str, options: Sequence[str], default: int) -> int:
        for i, label in enumerate(options, 1):
            self.say(f"  {i}. {label}")
        while True:
            try:
                answer = self.io.ask(f"{question} [{default + 1}] ").strip()
            except EOFError:
                return default
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            self.say(f"  type a number from 1 to {len(options)}")

    # checks
    def cli(self, spec: CliSpec, *, agent: bool) -> CliState | None:
        state = detect_cli(spec, self.io.which, self.io.run, self.io.environ)
        if not state.installed:
            self.say(f"  {spec.label} (`{spec.name}`) is not installed.")
            self.say(f"  Official install command: {spec.install}")
            if self.confirm("  Run it now?"):
                self.io.run_interactive(["sh", "-c", spec.install])
                state = detect_cli(spec, self.io.which, self.io.run, self.io.environ)
            if not state.installed:
                self.problems.append(
                    f"install {spec.label}: `{spec.install}` (then open a new terminal "
                    f"so `{spec.name}` is on PATH, and run `sombra setup` again)"
                )
                return None
        problem = state.problem(spec, agent=agent)
        if problem is not None:
            self.say(f"  {spec.label} at {state.path}: {problem}.")
            if self.confirm(f"  Fix it now with `{spec.update}`?"):
                self.io.run_interactive(["sh", "-c", spec.update])
                state = detect_cli(spec, self.io.which, self.io.run, self.io.environ)
                problem = state.problem(spec, agent=agent)
            if problem is not None:
                self.problems.append(f"{spec.label}: {problem}; run `{spec.update}`")
                return None
        assert state.path is not None and state.version is not None  # noqa: S101 - checked
        self.say(f"  ok  {spec.label} {version_str(state.version)} ({state.path})")
        return state

    def login(self, spec: CliSpec, path: str, want_subscription: bool) -> bool:
        state = login_state(spec, path, self.io.run, self.io.environ)
        if state in (Login.NONE, Login.UNKNOWN):
            what = "is not logged in" if state is Login.NONE else "did not report a login"
            self.say(f"  {spec.label} {what}.")
            argv = [path, *spec.login]
            if self.confirm(
                f"  Start its own login now (`{spec.name} {' '.join(spec.login)}`)? "
                "Sombra never sees the token."
            ):
                self.io.run_interactive(argv)
                state = login_state(spec, path, self.io.run, self.io.environ)
        if state in (Login.NONE, Login.UNKNOWN):
            self.problems.append(f"log in to {spec.label}: `{spec.login_hint}`")
            return False
        if want_subscription and state is not Login.SUBSCRIPTION:
            how = "an API key" if state is Login.API_KEY else "a non-subscription account"
            self.say(
                f"  note {spec.label} is logged in with {how}: that account is billed, "
                "not your subscription. Log out and back in with your plan to change it."
            )
        self.say(f"  ok  {spec.label} logged in ({state.value})")
        return True

    def key(self, provider: str) -> bool:
        try:
            stored = self.io.has_key(provider)
        except Exception as e:
            self.problems.append(f"unlock the OS keychain ({type(e).__name__}), then retry")
            return False
        if not stored:
            self.say(f"  No {provider} API key in the OS keychain.")
            if self.confirm(f"  Store one now (`sombra auth set {provider}`)?", default=True):
                self.io.set_key(provider)
                stored = _safe_has(self.io.has_key, provider)
        if not stored:
            self.problems.append(f"store the {provider} API key: `sombra auth set {provider}`")
            return False
        self.say(f"  ok  {provider} API key in the OS keychain")
        return True

    def needs(self, needs: Needs) -> bool:
        self.say(f"{needs.role.capitalize()}: {needs.backend}")
        spec = needs.cli
        path: str | None = None
        if spec is not None:
            state = self.cli(spec, agent=needs.role == "agent")
            if state is None:
                return False
            path = state.path
        if spec is not None and path is not None and needs.key_or_login:
            if needs.key is not None and _safe_has(self.io.has_key, needs.key):
                self.say(f"  ok  {needs.key} API key in the OS keychain")
                return True
            return self.login(spec, path, want_subscription=False)
        ok = True
        if spec is not None and path is not None and needs.login:
            ok = self.login(spec, path, want_subscription=needs.role == "agent")
        if needs.key is not None:
            ok = self.key(needs.key) and ok
        return ok


def _safe_has(has_key: Callable[[str], bool], provider: str) -> bool:
    try:
        return has_key(provider)
    except Exception:
        return False


def _current(path: Path) -> UserConfig | None:
    try:
        return load_user_config(path)
    except ConfigError:
        return None


def show(req: SetupRequest, io: SetupIO) -> int:
    try:
        cfg = load_user_config(req.config)
    except ConfigError as e:
        io.out.write(f"sombra setup: {e}\n")
        return 2
    summary = cfg.summary.resolve(cfg.brain)
    io.out.write(f"config:    {req.config}{'' if req.config.exists() else ' (not written yet)'}\n")
    io.out.write(f"agent:     {describe(cfg.brain)}\n")
    io.out.write(f"model:     {cfg.models.agent} (summaries: {cfg.models.summary})\n")
    follow = " (follows the agent)" if cfg.summary.backend == SUMMARY_FOLLOW else ""
    io.out.write(f"summaries: {summary or 'none (no rolling summaries or minutes)'}{follow}\n")
    for spec in (CLAUDE, CODEX):
        state = detect_cli(spec, io.which, io.run, io.environ)
        if state.path is None:
            io.out.write(f"{spec.name + ':':<11}not installed\n")
            continue
        version = version_str(state.version) if state.version else "unknown version"
        login = login_state(spec, state.path, io.run, io.environ).value
        io.out.write(f"{spec.name + ':':<11}{version}, login: {login} ({state.path})\n")
    return 0


def run_setup(req: SetupRequest, io: SetupIO) -> int:
    """The wizard. 0: configured and answering; 1: written but something is left; 2: error."""
    if req.show:
        return show(req, io)
    w = _Wizard(req, io)
    if req.config.exists() and _current(req.config) is None:
        try:
            load_user_config(req.config)
        except ConfigError as e:
            w.say(f"sombra setup: fix the config first: {e}")
        return 2
    current = _current(req.config) or UserConfig()

    backend, auth = req.backend, req.auth
    if backend is None:
        w.say("Which agent should answer in your meetings?")
        default = _default_choice(current.brain)
        _, backend, auth = CHOICES[w.pick("Choice", [c[0] for c in CHOICES], default)]
    auth = auth_for(backend, auth)
    brain = BrainConfig(backend=backend, auth=auth if backend == "codex" else None)

    summary = req.summary
    if summary is None:
        summary = current.summary.backend
        if req.interactive:
            summary = _ask_summary(w, brain, summary)
    summary_backend = SummaryConfig(summary).resolve(brain)

    w.say("")
    a_needs = agent_needs(brain)
    ready = w.needs(a_needs)
    s_needs = summary_needs(summary_backend)
    # Checked separately only when it needs something else: a CLI's summaries use its
    # own login even when the agent uses an API key (ADR 0046).
    if s_needs is not None and replace(s_needs, role=a_needs.role) != a_needs:
        ready = w.needs(s_needs) and ready

    try:
        write_provider(req.config, backend, brain.auth, summary)
    except (ConfigError, OSError) as e:
        w.say(f"sombra setup: could not write {req.config}: {e}")
        return 2
    w.say("")
    w.say(f"Wrote {req.config}: agent = {describe(brain)}; summaries = {summary_backend or 'none'}")

    if not ready:
        w.say("Left to do, then run `sombra setup` again:")
        for p in w.problems:
            w.say(f"  - {p}")
        return 1
    if not req.smoke:
        return 0
    return _smoke(w, req)


def _default_choice(brain: BrainConfig) -> int:
    """The current setting, so pressing Enter keeps it."""
    wants_key = bool(brain.uses_api_key)
    for i, (_, backend, auth) in enumerate(CHOICES):
        if backend == brain.backend and (backend != "codex" or (auth == AUTH_API_KEY) == wants_key):
            return i
    return 0


def _ask_summary(w: _Wizard, brain: BrainConfig, current: str) -> str:
    follow = SummaryConfig(SUMMARY_FOLLOW).resolve(brain)
    note = ""
    if brain.backend == "codex" and brain.auth == AUTH_API_KEY:
        note = " (through `codex login`: summaries never get the API key)"
    if w.confirm(
        f"Rolling summaries and minutes: use {follow} too{note}?",
        default=current == SUMMARY_FOLLOW,
    ):
        return SUMMARY_FOLLOW
    options = [b for b in SUMMARY_BACKENDS if b != SUMMARY_FOLLOW]
    labels = [BACKEND_LABEL.get(b, "no rolling summaries or minutes") for b in options]
    labels = [f"{b}: {label}" for b, label in zip(options, labels, strict=True)]
    default = options.index(current) if current in options else 0
    return options[w.pick("Summaries", labels, default)]


def _smoke(w: _Wizard, req: SetupRequest) -> int:
    cfg = load_user_config(req.config)
    w.say(f"Asking {cfg.brain.backend} one test question…")
    started = w.io.clock()
    try:
        answer = w.io.smoke(cfg)
    except Exception as e:  # any backend failure: say what it was, never a traceback
        w.say(f"  FAIL the agent did not answer ({type(e).__name__}): {e}")
        w.say("  Your choice is saved; fix the above and run `sombra doctor --live`.")
        return 1
    elapsed = w.io.clock() - started
    first = (answer.strip().splitlines() or [""])[0][:120]
    w.say(f"  ok  answered in {elapsed:.1f} s: {first}")
    w.say('Ready. Next: `sombra start "Daily"` (macOS) or `sombra ask latest "…"`.')
    return 0


# --- the real machine ----------------------------------------------------------------------


def _run_interactive(argv: Sequence[str]) -> int:  # pragma: no cover - takes the terminal
    try:
        return subprocess.call(list(argv))  # noqa: S603 - fixed argv (official commands)
    except OSError:
        return 127


def _set_key(provider: str) -> int:  # pragma: no cover - prompts for a hidden key
    from sombra.cli import main

    return main(["auth", "set", provider])


def _has_key(provider: str) -> bool:  # pragma: no cover - reads the OS keychain
    from sombra.privacy.secrets import has_api_key

    return has_api_key(provider)


def _smoke_answer(cfg: UserConfig) -> str:  # pragma: no cover - calls the real agent
    from sombra.orchestrator.smoke import smoke_answer

    return smoke_answer(cfg, SMOKE_QUESTION)


def live_io() -> SetupIO:  # pragma: no cover - the real machine; the wizard is tested with fakes
    return SetupIO(
        out=sys.stdout,
        ask=input,
        which=shutil.which,
        run=run_quiet,
        run_interactive=_run_interactive,
        has_key=_has_key,
        set_key=_set_key,
        smoke=_smoke_answer,
        environ=os.environ,
    )


__all__ = [
    "AUTH_API_KEY",
    "AUTH_SUBSCRIPTION",
    "CHOICES",
    "SetupError",
    "SetupIO",
    "SetupRequest",
    "run_setup",
    "show",
    "write_provider",
]
