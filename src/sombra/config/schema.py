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


# The agent that answers (PRD C5), and who pays for it (see docs/providers.md):
#   claude-code  your logged-in Claude Code CLI, on a Claude Pro/Max subscription
#   claude-api   Claude over the Messages API, with an Anthropic key from the keychain
#   codex        your Codex CLI, on a ChatGPT plan (`codex login`) or an OpenAI key
BRAIN_BACKENDS = ("claude-code", "claude-api", "codex")
BACKEND_ALIASES = {"claude": "claude-api"}  # the pre-#47 name, kept for old config files
DEFAULT_BACKEND = "claude-api"

# How the backend is paid. claude-code is always a subscription and claude-api always
# a key; codex can be either.
AUTH_SUBSCRIPTION = "subscription"
AUTH_API_KEY = "api-key"
AUTH_MODES = (AUTH_SUBSCRIPTION, AUTH_API_KEY)
BACKEND_AUTH: dict[str, tuple[str, ...]] = {
    "claude-code": (AUTH_SUBSCRIPTION,),
    "claude-api": (AUTH_API_KEY,),
    "codex": AUTH_MODES,
}

# ``[summary] backend``: "follow" uses the agent's backend; "none" writes no rolling
# summaries or minutes.
SUMMARY_FOLLOW = "follow"
SUMMARY_NONE = "none"
SUMMARY_BACKENDS = (SUMMARY_FOLLOW, *BRAIN_BACKENDS, SUMMARY_NONE)


@dataclass(frozen=True, slots=True)
class BrainConfig:
    backend: str = DEFAULT_BACKEND  # one of BRAIN_BACKENDS (C5)
    # None: the backend's only mode; for codex, the pre-#47 behaviour (an OpenAI key
    # from the keychain when one is stored, else the CLI's own login).
    auth: str | None = None

    @property
    def uses_api_key(self) -> bool | None:
        """True / False when the mode is fixed; None for codex without ``auth``."""
        if self.auth is not None:
            return self.auth == AUTH_API_KEY
        modes = BACKEND_AUTH[self.backend]
        return modes[0] == AUTH_API_KEY if len(modes) == 1 else None


@dataclass(frozen=True, slots=True)
class SummaryConfig:
    backend: str = SUMMARY_FOLLOW  # one of SUMMARY_BACKENDS

    def resolve(self, brain: BrainConfig) -> str | None:
        """The backend that writes summaries and minutes, or None for ``none``."""
        if self.backend == SUMMARY_NONE:
            return None
        return brain.backend if self.backend == SUMMARY_FOLLOW else self.backend


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
    summary: SummaryConfig = field(default_factory=SummaryConfig)


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
