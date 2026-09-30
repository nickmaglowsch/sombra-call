"""TOML loaders (stdlib ``tomllib``) for the user config, profiles and ``meeting.toml``.

Every error is a ``ConfigError`` naming the file and the dotted key. Unknown keys
are errors too, so a typo does not silently fall back to a default. Keys that look
like secrets (``api_key``, ``token``, ``password``...) are rejected everywhere:
API keys live in the OS keychain, set with ``sombra auth``.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from sombra.config.schema import (
    BACKEND_ALIASES,
    BACKEND_AUTH,
    BRAIN_BACKENDS,
    DEFAULT_MEETINGS_ROOT,
    SUMMARY_BACKENDS,
    AudioConfig,
    BrainConfig,
    ConfigError,
    MeetingConfig,
    ModelsConfig,
    Profile,
    RetentionConfig,
    SummaryConfig,
    UserConfig,
    UserIdentity,
)
from sombra.contracts import AutonomyLevel

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|password|passwd|credential|private[_-]?key|(^|[_-])token$)",
    re.IGNORECASE,
)
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/sombra`` if set, else ``~/.config/sombra`` (also on macOS)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path("~/.config").expanduser()) / "sombra"


def default_config_path() -> Path:
    return config_dir() / "config.toml"


def profiles_dir() -> Path:
    return config_dir() / "profiles"


# --- reading helpers -----------------------------------------------------------------


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(path, "<file>", f"invalid TOML: {e}") from e
    _reject_secrets(path, data, "")
    return data


def _reject_secrets(path: Path, table: dict[str, Any], prefix: str) -> None:
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if _SECRET_KEY_RE.search(key):
            raise ConfigError(
                path,
                dotted,
                "secrets must not be stored in config files; "
                "run `sombra auth` to keep the API key in the OS keychain",
            )
        if isinstance(value, dict):
            _reject_secrets(path, value, f"{dotted}.")
        elif isinstance(value, list):  # arrays of tables: [[x]]
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    _reject_secrets(path, item, f"{dotted}[{i}].")


class _Table:
    """Typed, key-tracking view of one TOML table."""

    def __init__(self, path: Path, data: dict[str, Any], prefix: str = "") -> None:
        self.path, self.data, self.prefix = path, data, prefix
        self.seen: set[str] = set()

    def _err(self, key: str, message: str) -> ConfigError:
        return ConfigError(self.path, f"{self.prefix}{key}", message)

    def _get(self, key: str, check: Callable[[Any], bool], expected: str) -> Any:
        self.seen.add(key)
        if key not in self.data:
            return None
        value = self.data[key]
        if not check(value):
            raise self._err(key, f"expected {expected}, got {type(value).__name__} {value!r}")
        return value

    def get_str(self, key: str, default: str | None) -> str | None:
        value = self._get(key, lambda v: isinstance(v, str), "a string")
        return default if value is None else value

    def req_str(self, key: str) -> str:
        value = self.get_str(key, None)
        if value is None:
            raise self._err(key, "required key is missing")
        return value

    def get_int(self, key: str, default: int, *, minimum: int) -> int:
        value = self._get(
            key, lambda v: isinstance(v, int) and not isinstance(v, bool), "an integer"
        )
        if value is None:
            return default
        if value < minimum:
            raise self._err(key, f"must be >= {minimum}, got {value}")
        return int(value)

    def get_float(self, key: str, default: float, *, low: float, high: float) -> float:
        value = self._get(
            key, lambda v: isinstance(v, int | float) and not isinstance(v, bool), "a number"
        )
        if value is None:
            return default
        if not low <= value <= high:
            raise self._err(key, f"must be between {low} and {high}, got {value}")
        return float(value)

    def str_list(self, key: str) -> tuple[str, ...]:
        value = self._get(
            key,
            lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v),
            "a list of strings",
        )
        return () if value is None else tuple(value)

    def choice(self, key: str, default: str, allowed: tuple[str, ...]) -> str:
        value = self.get_str(key, None)
        if value is None:
            return default
        if value not in allowed:
            raise self._err(key, f"must be one of {', '.join(allowed)}, got {value!r}")
        return value

    def autonomy(self, key: str, default: AutonomyLevel) -> AutonomyLevel:
        value = self.get_str(key, None)
        if value is None:
            return default
        try:
            return AutonomyLevel(value.upper())
        except ValueError:
            allowed = ", ".join(level.value for level in AutonomyLevel)
            raise self._err(key, f"must be one of {allowed}, got {value!r}") from None

    def get_datetime(self, key: str) -> datetime:
        value = self._get(key, lambda v: isinstance(v, datetime), "a TOML datetime")
        if value is None:
            raise self._err(key, "required key is missing")
        return cast(datetime, value)

    def table(self, key: str) -> _Table:
        value = self._get(key, lambda v: isinstance(v, dict), "a table")
        return _Table(self.path, value or {}, f"{self.prefix}{key}.")

    def done(self) -> None:
        for key in self.data:
            if key not in self.seen:
                raise self._err(key, "unknown key")


# --- public loaders ------------------------------------------------------------------


def load_user_config(path: Path | None = None) -> UserConfig:
    """Load the user config; a missing file gives ``UserConfig()`` (documented defaults)."""
    path = default_config_path() if path is None else Path(path).expanduser()
    if not path.exists():
        return UserConfig(meetings_root=DEFAULT_MEETINGS_ROOT.expanduser())
    t = _Table(path, _read_toml(path))
    d = UserConfig()

    root = t.get_str("meetings_root", None)
    user_t = t.table("user")
    user = UserIdentity(
        name=user_t.get_str("name", d.user.name) or "", aliases=user_t.str_list("aliases")
    )
    ret_t = t.table("retention")
    retention = RetentionConfig(
        frames_days=ret_t.get_int("frames_days", d.retention.frames_days, minimum=1),
        transcripts_days=ret_t.get_int("transcripts_days", d.retention.transcripts_days, minimum=1),
    )
    audio_t = t.table("audio")
    audio = AudioConfig(mic=audio_t.get_str("mic", None), system=audio_t.get_str("system", None))
    models_t = t.table("models")
    models = ModelsConfig(
        stt=models_t.get_str("stt", d.models.stt) or d.models.stt,
        agent=models_t.get_str("agent", d.models.agent) or d.models.agent,
        summary=models_t.get_str("summary", d.models.summary) or d.models.summary,
    )
    brain_t = t.table("brain")
    brain = _brain(brain_t, d.brain)
    summary_t = t.table("summary")
    summary = SummaryConfig(
        backend=_backend(summary_t, "backend", d.summary.backend, SUMMARY_BACKENDS)
    )
    cfg = UserConfig(
        meetings_root=Path(root or DEFAULT_MEETINGS_ROOT).expanduser(),
        autonomy_level=t.autonomy("autonomy_level", d.autonomy_level),
        capture_interval_s=t.get_float(
            "capture_interval_s", d.capture_interval_s, low=1.0, high=60.0
        ),
        user=user,
        retention=retention,
        audio=audio,
        models=models,
        brain=brain,
        summary=summary,
    )
    for sub in (user_t, ret_t, audio_t, models_t, brain_t, summary_t, t):
        sub.done()
    return cfg


def _backend(t: _Table, key: str, default: str, allowed: tuple[str, ...]) -> str:
    """A backend name; ``claude`` (the pre-#47 name) reads as ``claude-api``."""
    value = t.get_str(key, None)
    if value is None:
        return default
    value = BACKEND_ALIASES.get(value, value)
    if value not in allowed:
        raise t._err(key, f"must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _brain(t: _Table, default: BrainConfig) -> BrainConfig:
    backend = _backend(t, "backend", default.backend, BRAIN_BACKENDS)
    modes = BACKEND_AUTH[backend]
    auth = t.get_str("auth", None)
    if auth is not None and auth not in modes:
        raise t._err(
            "auth", f"must be {' or '.join(repr(m) for m in modes)} for {backend}, got {auth!r}"
        )
    return BrainConfig(backend=backend, auth=auth)


def load_profile(name: str, directory: Path | None = None) -> Profile:
    """Load ``<directory>/<name>.toml``. Raises ``FileNotFoundError`` if there is none."""
    directory = profiles_dir() if directory is None else Path(directory).expanduser()
    if not _PROFILE_NAME_RE.match(name):
        raise ValueError(f"invalid profile name: {name!r}")
    path = directory / f"{name}.toml"
    if not path.is_file():
        raise FileNotFoundError(f"no profile {name!r} (looked for {path})")
    return load_profile_file(path)


def load_profile_file(path: Path) -> Profile:
    t = _Table(path, _read_toml(path))
    context = tuple(
        p if p.is_absolute() else path.parent / p
        for p in (Path(s).expanduser() for s in t.str_list("context"))
    )
    profile = Profile(
        name=path.stem,
        description=t.get_str("description", "") or "",
        context=context,
        allowed_topics=t.str_list("allowed_topics"),
    )
    t.done()
    return profile


def list_profiles(directory: Path | None = None) -> list[Profile]:
    """Every ``*.toml`` profile in ``directory``, sorted by name. Missing dir -> []."""
    directory = profiles_dir() if directory is None else Path(directory).expanduser()
    return [load_profile_file(p) for p in _profile_files(directory)]


def _profile_files(directory: Path) -> Iterator[Path]:
    if directory.is_dir():
        yield from sorted(p for p in directory.glob("*.toml") if p.is_file())


def load_meeting_config(path: Path) -> MeetingConfig:
    """Load ``meeting.toml`` (pass the file or the meeting folder)."""
    path = Path(path)
    if path.is_dir():
        path = path / "meeting.toml"
    t = _Table(path, _read_toml(path))
    cfg = MeetingConfig(
        name=t.req_str("name"),
        started_at=t.get_datetime("started_at"),
        aliases=t.str_list("aliases"),
        autonomy_level=t.autonomy("autonomy_level", AutonomyLevel.L1),
        allowed_topics=t.str_list("allowed_topics"),
        profile=t.get_str("profile", None),
        context_files=t.str_list("context_files"),
    )
    t.done()
    return cfg
