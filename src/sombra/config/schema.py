"""Typed config: the user config file, profiles (M2) and ``meeting.toml``.

Defaults live here and are documented in ``docs/config.md``; keep the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sombra.contracts import AutonomyLevel

DEFAULT_MEETINGS_ROOT = Path("~/Sombra/meetings")


class ConfigError(ValueError):
    """A config file is invalid. The message always names the file and the key."""

    def __init__(self, path: Path, key: str, message: str) -> None:
        super().__init__(f"{path}: {key}: {message}")
        self.path = path
        self.key = key
        self.message = message


@dataclass(frozen=True, slots=True)
class UserIdentity:
    name: str = ""
    aliases: tuple[str, ...] = ()  # names the trigger listens for; ``name`` is always included

    @property
    def all_aliases(self) -> tuple[str, ...]:
        names = ((self.name,) if self.name else ()) + self.aliases
        return tuple(dict.fromkeys(names))  # dedupe, keep order


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    frames_days: int = 7
    transcripts_days: int = 30


@dataclass(frozen=True, slots=True)
class AudioConfig:
    mic: str | None = None  # device id or name; None = system default input
    system: str | None = None  # loopback/tap device; None = platform default


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    stt: str = "large-v3-turbo"  # whisper.cpp model
    agent: str = "sonnet"  # agent backend model alias
    summary: str = "haiku"  # rolling summary / minutes model alias


BRAIN_BACKENDS = ("claude", "codex")


@dataclass(frozen=True, slots=True)
class BrainConfig:
    backend: str = "claude"  # which agent answers: one of BRAIN_BACKENDS (C5)


@dataclass(frozen=True, slots=True)
class UserConfig:
    """``~/.config/sombra/config.toml``. A missing file means all defaults."""

    meetings_root: Path = DEFAULT_MEETINGS_ROOT  # the loader expands ~
    autonomy_level: AutonomyLevel = AutonomyLevel.L1
    capture_interval_s: float = 5.0
    user: UserIdentity = field(default_factory=UserIdentity)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)


@dataclass(frozen=True, slots=True)
class Profile:
    """``~/.config/sombra/profiles/<name>.toml``: reusable context for a kind of meeting."""

    name: str
    description: str = ""
    context: tuple[Path, ...] = ()  # absolute; relative paths resolve against the profile dir
    allowed_topics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MeetingConfig:
    """``<meeting>/meeting.toml``, written by ``store.create_meeting``."""

    name: str
    started_at: datetime
    aliases: tuple[str, ...] = ()
    autonomy_level: AutonomyLevel = AutonomyLevel.L1
    allowed_topics: tuple[str, ...] = ()
    profile: str | None = None
    context_files: tuple[str, ...] = ()
