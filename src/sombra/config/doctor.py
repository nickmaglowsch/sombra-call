"""``sombra doctor``: is this machine ready to run Sombra, and what is left to do.

Every check is independent and returns ok / warn / fail with a one-line fix; any fail
makes ``sombra doctor`` exit 1. Checks never talk to the OS directly: they read a
``Probes`` bundle, so they are unit-tested with fakes, and ``live_probes()`` builds
the real one (pyobjc, ``keyring``, ``sounddevice`` imported lazily, never prompting).

Sections: ``sombra`` (versions, models, disk, keychain), ``macos`` (which app holds the
TCC grants and whether the app's signature keeps them, the permissions, input device,
system-audio route) or ``linux`` (live capture
not built yet, ADR 0016), and ``agent`` (CLIs on PATH and API keys). The agent section
is deliberately small and self-contained: the provider wizard (R4) replaces it with
per-backend checks.

This lives in ``config`` because it inspects several packages (``transcription``,
``privacy``, ``audio``) and ``config`` is a wiring package allowed to import them.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import sombra


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


@dataclass(frozen=True, slots=True)
class TccHolder:
    """The app macOS attributes Sombra's permission requests to (its responsible process)."""

    name: str
    bundle_id: str = ""
    path: str = ""  # the outermost .app, or the executable when it is in no bundle


# Sombra.app's bundle id (packaging/macos/sombra_app.py, ADR 0050). TCC keys the grants on
# it, so it never changes; a test checks both copies agree.
SOMBRA_BUNDLE_ID = "io.github.nickmaglowsch.Sombra"
SHIM_RELPATH = "Contents/Helpers/sombra"

MIN_MACOS = (14, 0)
UBUNTU_VERSIONS = ("22.04", "24.04")
DISK_FAIL_BYTES = 1 << 30  # 1 GiB: not enough for a meeting's frames
DISK_WARN_BYTES = 5 << 30
AGENT_CLIS = ("claude", "codex")
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
    host_app: str  # the app macOS grants permissions to (Sombra.app, or the terminal)
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
    # Who macOS holds responsible for this process; None when it cannot be read.
    tcc_holder: TccHolder | None = None
    running_app: str = ""  # the Sombra.app this sombra runs from; "" for the uv install
    # That app's signature: "certificate:<name>", "adhoc", "unsigned", or "" (unknown).
    app_signature: str = ""


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


def check_permission_holder(p: Probes) -> list[Check]:
    """Which app holds Microphone / Screen Recording / Accessibility (ADR 0050)."""
    name = "permissions holder"
    holder = p.tcc_holder
    if holder is None:
        return [
            Check(
                "macos",
                name,
                Status.WARN,
                f"could not read the responsible process; probably {p.host_app}",
            )
        ]
    where = f"{holder.name} ({holder.bundle_id})" if holder.bundle_id else holder.name
    if holder.path:
        where += f" at {holder.path}"
    if holder.bundle_id == SOMBRA_BUNDLE_ID:
        return [Check("macos", name, Status.OK, f"{where}: the grants belong to Sombra alone")]
    if p.running_app:
        return [
            Check(
                "macos",
                name,
                Status.WARN,
                f"{where}, although sombra runs from {p.running_app}",
                f"run sombra through the CLI shim {p.running_app}/{SHIM_RELPATH} "
                "(install.sh links it onto PATH) so macOS asks for the grants as Sombra",
            )
        ]
    return [
        Check(
            "macos",
            name,
            Status.OK,
            f"{where}: sombra runs as a command-line tool, so its grants cover every "
            "program run from there",
            "install Sombra.app (install.sh does by default on macOS) so the grants "
            "belong to Sombra alone",
        )
    ]


def check_app_signature(p: Probes) -> list[Check]:
    """Whether Sombra.app's signature lets macOS keep the grants across upgrades (ADR 0050)."""
    if not p.running_app:
        return []  # the uv install: the terminal's signature is what counts
    name = "app signature"
    kind, _, who = p.app_signature.partition(":")
    if kind == "certificate":
        return [
            Check(
                "macos",
                name,
                Status.OK,
                f"signed with the certificate {who!r}: the grants survive upgrades",
            )
        ]
    if kind == "adhoc":
        return [
            Check(
                "macos",
                name,
                Status.WARN,
                "ad-hoc signature (a development build or a labelled pre-release): macOS "
                "forgets the permission grants on every upgrade",
                "install a final release, signed with Sombra's certificate (install.sh)",
            )
        ]
    return [
        Check(
            "macos",
            name,
            Status.WARN,
            f"could not read the signature of {p.running_app}",
            f"run `codesign -dv {p.running_app}`; reinstall Sombra.app if it fails",
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
    ("permissions holder", check_permission_holder),
    ("app signature", check_app_signature),
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


# Agent section: kept apart on purpose; R4 (`sombra setup`) replaces it.
_CLI_FIX = {
    "claude": "install Claude Code (https://claude.com/claude-code) and run `claude` once "
    "to log in",
    "codex": "install the Codex CLI (`npm i -g @openai/codex`) and run `codex login`",
}


def check_agent(p: Probes) -> list[Check]:
    out = []
    for cli in AGENT_CLIS:
        path = p.which(cli)
        if path:
            out.append(Check("agent", f"{cli} CLI", Status.OK, path))
        else:
            out.append(Check("agent", f"{cli} CLI", Status.WARN, "not on PATH", _CLI_FIX[cli]))
    for provider in AGENT_KEYS:
        name = f"{provider} API key"
        try:
            stored = p.has_key(provider)
        except Exception:
            out.append(
                Check("agent", name, Status.WARN, "keychain unreachable", "see the keychain check")
            )
            continue
        if stored:
            out.append(Check("agent", name, Status.OK, "stored in the keychain"))
        else:
            out.append(
                Check("agent", name, Status.WARN, "not set", f"`sombra auth set {provider}`")
            )
    return out


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


def app_bundle_of(executable: str) -> str | None:
    """The outermost ``.app`` bundle containing ``executable``, or None.

    Outermost, because a helper inside an app (``Code Helper.app`` in ``Visual Studio
    Code.app``) is shown in System Settings as the app that ships it.
    """
    parts = Path(executable).parts
    for i, part in enumerate(parts):
        if part.endswith(".app"):
            return str(Path(*parts[: i + 1]))
    return None


def read_bundle_info(bundle: Path) -> Mapping[str, Any]:
    with (bundle / "Contents" / "Info.plist").open("rb") as f:
        info: Mapping[str, Any] = plistlib.load(f)
    return info


def holder_from_path(
    executable: str, read_info: Callable[[Path], Mapping[str, Any]] = read_bundle_info
) -> TccHolder:
    """Name the app System Settings lists for the responsible process ``executable``."""
    bundle = app_bundle_of(executable)
    if bundle is None:
        return TccHolder(Path(executable).name or executable, "", executable)
    try:
        info = read_info(Path(bundle))
    except (OSError, plistlib.InvalidFileException, ValueError):
        info = {}
    name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or Path(bundle).stem
    return TccHolder(str(name), str(info.get("CFBundleIdentifier", "")), bundle)


def parse_signature(codesign_output: str) -> str:
    """``codesign -dv`` output -> "certificate:<leaf name>", "adhoc", "unsigned" or ""."""
    if "not signed at all" in codesign_output:
        return "unsigned"
    authority = ""
    for line in codesign_output.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "Signature" and value.strip() == "adhoc":
            return "adhoc"
        if key == "Authority" and not authority:
            authority = value.strip()
    return f"certificate:{authority}" if authority else ""


def running_app(frozen: bool, executable: str) -> str:
    """The Sombra.app this interpreter runs from (PyInstaller build), or ""."""
    if not frozen:
        return ""
    return app_bundle_of(executable) or ""


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


def _mac_responsible_path() -> str | None:  # pragma: no cover - macOS only
    """The executable of the process TCC holds responsible for this one.

    ``responsibility_get_pid_responsible_for_pid`` is private SPI in libSystem (it is
    what TCC itself consults); ``proc_pidpath`` is public. None if either is missing.
    """
    libc = ctypes.CDLL(None)
    responsible = getattr(libc, "responsibility_get_pid_responsible_for_pid", None)
    if responsible is None:
        return None
    responsible.restype = ctypes.c_int
    responsible.argtypes = [ctypes.c_int]
    pid = responsible(os.getpid())
    if pid <= 0:
        return None
    buf = ctypes.create_string_buffer(4096)
    libc.proc_pidpath.restype = ctypes.c_int
    libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    n = libc.proc_pidpath(pid, buf, ctypes.sizeof(buf))
    return buf.value.decode("utf-8", "replace") if n > 0 else None


def _mac_tcc_holder() -> TccHolder | None:  # pragma: no cover - macOS only
    try:
        path = _mac_responsible_path()
    except Exception:  # a probe never crashes doctor; the check reports "could not read"
        return None
    return holder_from_path(path) if path else None


def _mac_app_signature(app: str) -> str:  # pragma: no cover - macOS only
    try:
        proc = subprocess.run(  # noqa: S603  # fixed argv: the system codesign, our own path
            ["/usr/bin/codesign", "-dv", "--verbose=2", app],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return parse_signature(proc.stdout + proc.stderr)


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
    config_path: Path | None = None, models_dir: Path | None = None
) -> Probes:  # pragma: no cover - reads the real machine; checks are tested with fakes
    from sombra.config.loader import load_user_config
    from sombra.config.schema import DEFAULT_MEETINGS_ROOT, ConfigError
    from sombra.transcription.models import SILERO_VAD, default_models_dir, whisper_model

    try:
        cfg = load_user_config(config_path)
        data_dir = cfg.meetings_root
        stt = cfg.models.stt
    except ConfigError:
        data_dir, stt = DEFAULT_MEETINGS_ROOT.expanduser(), ""
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
    app = running_app(bool(getattr(sys, "frozen", False)), sys.executable)
    holder = None
    signature = ""
    if sys.platform == "darwin":
        release = platform.mac_ver()[0]
        os_name = "macOS"
        holder = _mac_tcc_holder()
        signature = _mac_app_signature(app) if app else ""
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
        host_app=holder.name if holder else host_app(os.environ),
        tcc_holder=holder,
        running_app=app,
        app_signature=signature,
        sha256=file_sha256,
        disk_free=lambda path: shutil.disk_usage(path).free,
        which=shutil.which,
        has_key=_has_key,
        **extra,
    )
