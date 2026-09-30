"""``sombra doctor``: is this machine ready to run Sombra, and what is left to do.

Every check is independent and returns ok / warn / fail with a one-line fix; any fail
makes ``sombra doctor`` exit 1. Checks never talk to the OS directly: they read a
``Probes`` bundle, so they are unit-tested with fakes, and ``live_probes()`` builds
the real one (pyobjc, ``keyring``, ``sounddevice`` imported lazily, never prompting).

Sections: ``sombra`` (versions, models, disk, keychain), ``macos`` (TCC permissions,
input device, system-audio route) or ``linux`` (live capture not built yet, ADR 0016),
and ``agent``: the configured backend (``[brain]``) and summary backend (``[summary]``),
each with what it needs: the CLI, its minimum version and its login (from the CLI's own
status command, ``config.providers``), or the API key in the keychain. ``--live`` adds one
test question through the agent (``orchestrator.smoke``).

This lives in ``config`` because it inspects several packages (``transcription``,
``privacy``, ``audio``) and ``config`` is a wiring package allowed to import them.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import os
import platform
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import sombra
from sombra.config.providers import (
    CliSpec,
    CliState,
    Login,
    Needs,
    ProcResult,
    Runner,
    agent_needs,
    describe,
    detect_cli,
    login_state,
    run_quiet,
    summary_needs,
    version_str,
)
from sombra.config.schema import DEFAULT_BACKEND, BrainConfig


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class Permission(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    NOT_DETERMINED = "not_determined"  # macOS will ask the first time it is used
    UNKNOWN = "unknown"  # could not be probed without prompting


@dataclass(frozen=True, slots=True)
class Check:
    section: str
    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A model file doctor expects in the models folder."""

    label: str
    filename: str
    sha256: str
    download: str = "sombra models download"  # the command that fetches it


MIN_MACOS = (14, 0)
UBUNTU_VERSIONS = ("22.04", "24.04")
DISK_FAIL_BYTES = 1 << 30  # 1 GiB: not enough for a meeting's frames
DISK_WARN_BYTES = 5 << 30
AGENT_KEYS = ("anthropic", "openai")  # `sombra auth set <provider>`


@dataclass(slots=True)
class Probes:
    """Everything the checks read from the machine. Tests pass fakes."""

    platform: str  # sys.platform: "darwin", "linux", ...
    os_version: str  # macOS product version ("14.4.1") or Ubuntu VERSION_ID ("24.04")
    os_name: str  # "macOS", "ubuntu", ...
    machine: str  # "arm64", "x86_64", "aarch64"
    sombra_version: str
    python_version: str
    models_dir: Path
    models: tuple[ModelSpec, ...]
    data_dir: Path  # where meetings go; its disk is the one that fills up
    host_app: str  # the app macOS grants permissions to (the terminal sombra runs in)
    sha256: Callable[[Path], str]
    disk_free: Callable[[Path], int]
    which: Callable[[str], str | None]
    # True/False: a key is (not) stored; raises when the keychain is unreachable.
    has_key: Callable[[str], bool]
    mic_permission: Callable[[], Permission] = lambda: Permission.UNKNOWN
    screen_permission: Callable[[], Permission] = lambda: Permission.UNKNOWN
    accessibility_permission: Callable[[], Permission] = lambda: Permission.UNKNOWN
    default_input: Callable[[], str | None] = lambda: None
    # "tap", or the loopback device's name, or None when there is no route
    system_audio: Callable[[], str | None] = lambda: None
    # The agent section: the configured provider, and the CLIs' --version / status.
    brain: BrainConfig = field(default_factory=BrainConfig)
    summary_backend: str | None = DEFAULT_BACKEND
    run: Runner = lambda argv, env: ProcResult(127, "", "")
    environ: Mapping[str, str] = field(default_factory=dict)
    smoke: Callable[[], str] | None = None  # `doctor --live`: one answer, or raises


# --- checks --------------------------------------------------------------------------


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for p in text.split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts)


def check_versions(p: Probes) -> list[Check]:
    py = _version_tuple(p.python_version)
    py_ok = py[:2] >= (3, 12)
    return [
        Check("sombra", "sombra", Status.OK, f"sombra {p.sombra_version}"),
        Check(
            "sombra",
            "python",
            Status.OK if py_ok else Status.FAIL,
            f"Python {p.python_version}",
            "" if py_ok else "reinstall with scripts/install.sh (it uses a uv-managed 3.12)",
        ),
    ]


def check_models(p: Probes) -> list[Check]:
    out = []
    for m in p.models:
        path = p.models_dir / m.filename
        if not path.is_file():
            out.append(
                Check(
                    "sombra",
                    f"model {m.label}",
                    Status.FAIL,
                    f"missing: {path}",
                    f"run `{m.download}`",
                )
            )
            continue
        actual = p.sha256(path)
        if actual != m.sha256:
            out.append(
                Check(
                    "sombra",
                    f"model {m.label}",
                    Status.FAIL,
                    f"checksum mismatch: {path}",
                    f"delete {path} and run `{m.download}`",
                )
            )
        else:
            out.append(Check("sombra", f"model {m.label}", Status.OK, f"{path} (sha256 ok)"))
    return out


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path(path.anchor or "/")


def check_disk(p: Probes) -> list[Check]:
    where = _existing_ancestor(p.data_dir)
    free = p.disk_free(where)
    gib = free / (1 << 30)
    detail = f"{gib:.1f} GiB free on {where}"
    fix = "free some space: meetings keep frames for 7 days (`sombra retention run`)"
    if free < DISK_FAIL_BYTES:
        return [Check("sombra", "disk", Status.FAIL, detail, fix)]
    if free < DISK_WARN_BYTES:
        return [Check("sombra", "disk", Status.WARN, detail, fix)]
    return [Check("sombra", "disk", Status.OK, detail)]


def _keychain_name(p: Probes) -> str:
    return "Keychain" if p.platform == "darwin" else "Secret Service"


def check_keychain(p: Probes) -> list[Check]:
    name = _keychain_name(p)
    try:
        p.has_key(AGENT_KEYS[0])
    except Exception as e:
        fix = (
            "unlock the login keychain (Keychain Access)"
            if p.platform == "darwin"
            else "install and unlock a Secret Service provider (gnome-keyring on Ubuntu)"
        )
        reason = type(e).__name__
        return [Check("sombra", "keychain", Status.WARN, f"{name} unreachable ({reason})", fix)]
    return [Check("sombra", "keychain", Status.OK, f"{name} reachable")]


def _grant(pane: str, app: str) -> str:
    return f"System Settings > Privacy & Security > {pane}: enable {app}, then restart {app}"


def _permission_check(name: str, pane: str, state: Permission, app: str, why: str) -> Check:
    fix = _grant(pane, app)
    if state is Permission.GRANTED:
        return Check("macos", name, Status.OK, f"granted to {app}")
    if state is Permission.DENIED:
        return Check("macos", name, Status.FAIL, f"denied for {app}; {why}", fix)
    if state is Permission.NOT_DETERMINED:
        return Check(
            "macos",
            name,
            Status.WARN,
            f"not asked yet; macOS prompts {app} the first time; {why}",
            f"allow the prompt, or {fix}",
        )
    return Check("macos", name, Status.WARN, f"not granted or not checkable; {why}", fix)


def check_macos_version(p: Probes) -> list[Check]:
    v = _version_tuple(p.os_version)
    if v and v >= MIN_MACOS:
        return [Check("macos", "macOS", Status.OK, f"macOS {p.os_version} ({p.machine})")]
    return [
        Check(
            "macos",
            "macOS",
            Status.FAIL,
            f"macOS {p.os_version or '?'}; Sombra needs 14 (Sonoma) or newer",
            "update macOS",
        )
    ]


def check_microphone(p: Probes) -> list[Check]:
    why = "needed for your voice (EU)"
    return [_permission_check("microphone", "Microphone", p.mic_permission(), p.host_app, why)]


def check_screen_recording(p: Probes) -> list[Check]:
    why = "needed for screenshots and window titles"
    state = p.screen_permission()
    return [_permission_check("screen recording", "Screen Recording", state, p.host_app, why)]


def check_accessibility(p: Probes) -> list[Check]:
    why = "needed for the global pause shortcut"
    state = p.accessibility_permission()
    return [_permission_check("accessibility", "Accessibility", state, p.host_app, why)]


def check_input_device(p: Probes) -> list[Check]:
    mic = p.default_input()
    if mic:
        return [Check("macos", "input device", Status.OK, f"default input: {mic}")]
    return [
        Check(
            "macos",
            "input device",
            Status.WARN,
            "no microphone found",
            "connect a microphone, or pick one with `sombra devices` and audio.mic",
        )
    ]


def check_system_audio(p: Probes) -> list[Check]:
    route = p.system_audio()
    if route == "tap":
        return [
            Check(
                "macos",
                "system audio",
                Status.OK,
                "Core Audio process tap (macOS 14.4+)",
                f"{p.host_app} is asked for System Audio Recording on the first meeting",
            )
        ]
    if route:
        return [Check("macos", "system audio", Status.OK, f"loopback device: {route}")]
    return [
        Check(
            "macos",
            "system audio",
            Status.WARN,
            "no route for other participants' audio (OUTROS)",
            "update to macOS 14.4+ for the process tap, or install BlackHole 2ch",
        )
    ]


MACOS_CHECKS: tuple[tuple[str, Callable[[Probes], list[Check]]], ...] = (
    ("macOS", check_macos_version),
    ("microphone", check_microphone),
    ("screen recording", check_screen_recording),
    ("accessibility", check_accessibility),
    ("input device", check_input_device),
    ("system audio", check_system_audio),
)


def check_linux(p: Probes) -> list[Check]:
    out = []
    if p.os_name == "ubuntu" and p.os_version in UBUNTU_VERSIONS:
        out.append(Check("linux", "distribution", Status.OK, f"Ubuntu {p.os_version}"))
    else:
        out.append(
            Check(
                "linux",
                "distribution",
                Status.WARN,
                f"{p.os_name or 'unknown'} {p.os_version}: only Ubuntu 22.04/24.04 is tested",
            )
        )
    out.append(
        Check(
            "linux",
            "live capture",
            Status.WARN,
            "not supported yet on Linux (ADR 0016); replay, ask, minutes and report work",
            "use macOS for live meetings until the Wayland/PipeWire adapters land",
        )
    )
    return out


# --- agent section ----------------------------------------------------------------------


def _cli_checks(p: Probes, spec: CliSpec, agent: bool) -> tuple[list[Check], CliState | None]:
    name = f"{spec.name} CLI"
    state = detect_cli(spec, p.which, p.run, p.environ)
    if state.path is None:
        fix = f"install it: `{spec.install}`, then run `sombra setup`"
        return [Check("agent", name, Status.FAIL, "not on PATH", fix)], None
    problem = state.problem(spec, agent=agent)
    if problem is not None:
        detail = f"{problem} ({state.path})"
        return [Check("agent", name, Status.FAIL, detail, f"`{spec.update}`")], None
    version = version_str(state.version) if state.version else "?"
    return [Check("agent", name, Status.OK, f"{version} ({state.path})")], state


def _login_check(p: Probes, spec: CliSpec, path: str, want_subscription: bool) -> Check:
    name = f"{spec.name} login"
    state = login_state(spec, path, p.run, p.environ)
    fix = f"`{spec.login_hint}`"
    if state is Login.NONE:
        return Check("agent", name, Status.FAIL, "not logged in", fix)
    if state is Login.UNKNOWN:
        return Check("agent", name, Status.WARN, "the CLI did not report a login state", fix)
    if want_subscription and state is not Login.SUBSCRIPTION:
        how = "an API key" if state is Login.API_KEY else "a non-subscription account"
        return Check(
            "agent",
            name,
            Status.WARN,
            f"logged in with {how}: that account is billed, not your subscription",
            f"log out and back in with your plan: `{spec.login_hint}`",
        )
    return Check("agent", name, Status.OK, f"logged in ({state.value})")


def _key_check(p: Probes, provider: str, status: Status = Status.FAIL) -> Check:
    name = f"{provider} API key"
    try:
        stored = p.has_key(provider)
    except Exception:
        return Check("agent", name, Status.WARN, "keychain unreachable", "see the keychain check")
    if stored:
        return Check("agent", name, Status.OK, "stored in the keychain")
    return Check("agent", name, status, "not set", f"`sombra auth set {provider}`")


def _needs_checks(p: Probes, needs: Needs, done: set[str]) -> list[Check]:
    out: list[Check] = []
    spec = needs.cli
    state: CliState | None = None
    if spec is not None:
        if spec.name not in done:
            cli, state = _cli_checks(p, spec, agent=needs.role == "agent")
            out += cli
            done.add(spec.name)
        else:
            state = detect_cli(spec, p.which, p.run, p.environ)
            state = state if state.usable(spec, agent=needs.role == "agent") else None
    path = state.path if state is not None else None
    if spec is not None and needs.key_or_login and needs.key is not None:
        key = _key_check(p, needs.key, Status.WARN)
        if key.status is Status.OK or path is None:
            return [*out, key]
        login = _login_check(p, spec, path, want_subscription=False)
        return [*out, key, login] if login.status is not Status.OK else [*out, login]
    if spec is not None and path is not None and needs.login and f"{spec.name}:login" not in done:
        out.append(_login_check(p, spec, path, want_subscription=needs.role == "agent"))
        done.add(f"{spec.name}:login")
    if needs.key is not None and f"key:{needs.key}" not in done:
        out.append(_key_check(p, needs.key))
        done.add(f"key:{needs.key}")
    return out


def check_agent(p: Probes) -> list[Check]:
    out = [Check("agent", "backend", Status.OK, describe(p.brain), "change it: `sombra setup`")]
    done: set[str] = set()
    out += _needs_checks(p, agent_needs(p.brain), done)
    s_needs = summary_needs(p.summary_backend)
    if s_needs is None:
        out.append(
            Check(
                "agent",
                "summaries",
                Status.WARN,
                "none: no rolling summaries or minutes",
                "set [summary] backend, or run `sombra setup`",
            )
        )
    else:
        out.append(Check("agent", "summaries", Status.OK, s_needs.backend))
        out += _needs_checks(p, s_needs, done)
    if p.smoke is not None:
        out.append(_smoke_check(p.smoke))
    return out


def _smoke_check(smoke: Callable[[], str]) -> Check:
    try:
        answer = smoke()
    except Exception as e:
        detail = f"no answer ({type(e).__name__}): {e}"[:300]
        return Check("agent", "live answer", Status.FAIL, detail, "fix the checks above")
    first = (answer.strip().splitlines() or [""])[0][:80]
    return Check("agent", "live answer", Status.OK, f"answered: {first}")


def _guarded(
    section: str, name: str, fn: Callable[[Probes], list[Check]], p: Probes
) -> list[Check]:
    """A probe that crashes is a warning about that check, never a crashed doctor."""
    try:
        return fn(p)
    except Exception as e:
        return [Check(section, name, Status.WARN, f"could not check: {type(e).__name__}: {e}")]


def run_checks(p: Probes) -> list[Check]:
    checks: list[Check] = []
    checks += _guarded("sombra", "versions", check_versions, p)
    checks += _guarded("sombra", "models", check_models, p)
    checks += _guarded("sombra", "disk", check_disk, p)
    checks += _guarded("sombra", "keychain", check_keychain, p)
    if p.platform == "darwin":
        for name, fn in MACOS_CHECKS:
            checks += _guarded("macos", name, fn, p)
    elif p.platform.startswith("linux"):
        checks += _guarded("linux", "linux", check_linux, p)
    else:
        checks.append(
            Check(
                "sombra",
                "platform",
                Status.FAIL,
                f"{p.platform} is not supported",
                "use macOS 14+ or Ubuntu 22.04/24.04",
            )
        )
    checks += _guarded("agent", "agent", check_agent, p)
    return checks


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status is Status.FAIL for c in checks) else 0


# --- rendering -----------------------------------------------------------------------

_MARK = {Status.OK: "ok  ", Status.WARN: "warn", Status.FAIL: "FAIL"}
_TITLE = {"sombra": "Sombra", "macos": "macOS", "linux": "Linux", "agent": "Agent"}


def render_text(checks: list[Check]) -> str:
    lines: list[str] = []
    section = None
    for c in checks:
        if c.section != section:
            section = c.section
            if lines:
                lines.append("")
            lines.append(_TITLE.get(section, section))
        lines.append(f"  [{_MARK[c.status]}] {c.name}: {c.detail}")
        if c.fix and c.status is not Status.OK:
            lines.append(f"         fix: {c.fix}")
        elif c.fix:
            lines.append(f"         note: {c.fix}")
    counts = {s: sum(c.status is s for c in checks) for s in Status}
    lines.append("")
    lines.append(f"{counts[Status.OK]} ok, {counts[Status.WARN]} warn, {counts[Status.FAIL]} fail")
    return "\n".join(lines) + "\n"


def render_json(checks: list[Check]) -> dict[str, Any]:
    return {
        "ok": exit_code(checks) == 0,
        "checks": [{k: str(v) for k, v in asdict(c).items()} for c in checks],
    }


# --- live probes ---------------------------------------------------------------------

_TERMINALS = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm",
    "vscode": "Visual Studio Code (or the VS Code fork you run)",
    "WarpTerminal": "Warp",
    "ghostty": "Ghostty",
    "WezTerm": "WezTerm",
    "Hyper": "Hyper",
    "tmux": "the terminal app running tmux",
}


def host_app(env: Mapping[str, str]) -> str:
    """The app macOS attributes our permission requests to: the terminal, not Python."""
    term = env.get("TERM_PROGRAM", "")
    if term in _TERMINALS:
        return _TERMINALS[term]
    bundle = env.get("__CFBundleIdentifier", "")
    if bundle:
        return bundle
    return "the app you run sombra from (Terminal, iTerm, your IDE)"


def read_os_release(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _has_key(provider: str) -> bool:
    from sombra.privacy.secrets import MissingApiKeyError, get_api_key

    try:
        get_api_key(provider)
    except MissingApiKeyError:
        return False
    return True


def _mac_mic_permission() -> Permission:  # pragma: no cover - macOS only
    objc: Any = importlib.import_module("objc")
    objc.loadBundle(
        "AVFoundation", {}, bundle_path="/System/Library/Frameworks/AVFoundation.framework"
    )
    device = objc.lookUpClass("AVCaptureDevice")
    # AVMediaTypeAudio == "soun"; this call reads the status and never prompts.
    status = int(device.authorizationStatusForMediaType_("soun"))
    return {
        0: Permission.NOT_DETERMINED,
        1: Permission.DENIED,  # restricted (parental controls / MDM)
        2: Permission.DENIED,
        3: Permission.GRANTED,
    }.get(status, Permission.UNKNOWN)


def _mac_screen_permission() -> Permission:  # pragma: no cover - macOS only
    quartz: Any = importlib.import_module("Quartz")
    # Preflight never prompts; False covers both "denied" and "not asked yet".
    return Permission.GRANTED if quartz.CGPreflightScreenCaptureAccess() else Permission.UNKNOWN


def _mac_accessibility_permission() -> Permission:  # pragma: no cover - macOS only
    lib = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    lib.AXIsProcessTrusted.restype = ctypes.c_bool
    return Permission.GRANTED if lib.AXIsProcessTrusted() else Permission.UNKNOWN


def _mac_default_input() -> str | None:  # pragma: no cover - needs PortAudio
    from sombra.audio.devices import default_input

    sd: Any = importlib.import_module("sounddevice")
    try:
        return default_input(sd.query_devices(), input_index(sd.default.device)).name
    except LookupError:
        return None


def input_index(device: Any) -> int:
    """The input half of ``sounddevice.default.device``, or -1 when unset.

    ``default.device`` is an ``_InputOutputPair`` (indexable, but not a tuple), a plain
    int, or None; the pair's items are an index or None/-1.
    """
    if device is None:
        return -1
    if isinstance(device, int):
        return device
    item = device[0]
    return -1 if item is None else int(item)


def _mac_system_audio(release: str) -> str | None:  # pragma: no cover - needs PortAudio
    from sombra.audio.devices import find_loopback, tap_supported

    if tap_supported(release):
        return "tap"
    sd: Any = importlib.import_module("sounddevice")
    loop = find_loopback(sd.query_devices())
    return loop.name if loop else None


def _whisper_model_name(configured: str) -> str:
    from sombra.transcription.models import DEFAULT_WHISPER_MODEL, WHISPER_MODELS

    return configured if configured in WHISPER_MODELS else DEFAULT_WHISPER_MODEL


def live_probes(
    config_path: Path | None = None, models_dir: Path | None = None, *, live: bool = False
) -> Probes:  # pragma: no cover - reads the real machine; checks are tested with fakes
    from sombra.config.loader import load_user_config
    from sombra.config.schema import DEFAULT_MEETINGS_ROOT, ConfigError, UserConfig
    from sombra.transcription.models import SILERO_VAD, default_models_dir, whisper_model

    try:
        cfg: UserConfig | None = load_user_config(config_path)
    except ConfigError:
        cfg = None
    if cfg is not None:
        data_dir, stt = cfg.meetings_root, cfg.models.stt
    else:
        data_dir, stt = DEFAULT_MEETINGS_ROOT.expanduser(), ""
    agent: dict[str, Any] = {}
    if cfg is not None:
        agent = {"brain": cfg.brain, "summary_backend": cfg.summary.resolve(cfg.brain)}
        if live:
            agent["smoke"] = lambda: _smoke(cfg)
    name = _whisper_model_name(stt)
    whisper = whisper_model(name)
    models = (
        ModelSpec(
            f"whisper {name}",
            whisper.filename,
            whisper.sha256,
            f"sombra models download {name}",
        ),
        ModelSpec("silero-vad", SILERO_VAD.filename, SILERO_VAD.sha256),
    )
    extra: dict[str, Any] = {}
    if sys.platform == "darwin":
        release = platform.mac_ver()[0]
        os_name = "macOS"
        extra = {
            "mic_permission": _mac_mic_permission,
            "screen_permission": _mac_screen_permission,
            "accessibility_permission": _mac_accessibility_permission,
            "default_input": _mac_default_input,
            "system_audio": lambda: _mac_system_audio(release),
        }
    else:
        try:
            info = read_os_release(Path("/etc/os-release").read_text(encoding="utf-8"))
        except OSError:
            info = {}
        release, os_name = info.get("VERSION_ID", ""), info.get("ID", "")
    return Probes(
        platform=sys.platform,
        os_version=release,
        os_name=os_name,
        machine=platform.machine(),
        sombra_version=sombra.__version__,
        python_version=platform.python_version(),
        models_dir=models_dir or default_models_dir(),
        models=models,
        data_dir=data_dir,
        host_app=host_app(os.environ),
        sha256=file_sha256,
        disk_free=lambda path: shutil.disk_usage(path).free,
        which=shutil.which,
        has_key=_has_key,
        run=run_quiet,
        environ=os.environ,
        **agent,
        **extra,
    )


def _smoke(cfg: Any) -> str:  # pragma: no cover - calls the real agent
    from sombra.config.setup import SMOKE_QUESTION
    from sombra.orchestrator.smoke import smoke_answer

    return smoke_answer(cfg, SMOKE_QUESTION)
