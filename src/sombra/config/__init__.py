"""User config, profiles (M2) and the meeting config schema.

Only the wiring layer (``orchestrator``, ``cli`` and this package's ``commands``)
reads config; feature modules get plain constructor arguments and must not import
this package. Keys and defaults are documented in ``docs/config.md``.
"""

from sombra.config.loader import (
    config_dir,
    default_config_path,
    list_profiles,
    load_meeting_config,
    load_profile,
    load_profile_file,
    load_user_config,
    profiles_dir,
)
from sombra.config.schema import (
    AudioConfig,
    BrainConfig,
    ConfigError,
    MeetingConfig,
    ModelsConfig,
    Profile,
    RetentionConfig,
    UserConfig,
    UserIdentity,
)

__all__ = [
    "AudioConfig",
    "BrainConfig",
    "ConfigError",
    "MeetingConfig",
    "ModelsConfig",
    "Profile",
    "RetentionConfig",
    "UserConfig",
    "UserIdentity",
    "config_dir",
    "default_config_path",
    "list_profiles",
    "load_meeting_config",
    "load_profile",
    "load_profile_file",
    "load_user_config",
    "profiles_dir",
]
