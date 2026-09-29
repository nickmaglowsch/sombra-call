"""Privacy and consent: what keeps Sombra safe to run on real calls.

Owns (PRD "Segurança, privacidade e LGPD"): the mandatory consent notice, the pause
shortcut and control socket, the blocked-apps list, retention of frames (7 d) and
transcript/summary/log (30 d), deleting a meeting, API keys in the OS keychain, and
the disk-encryption recommendation.

Every piece takes its settings as arguments (paths, retention days, app lists); the
orchestrator reads the user config and wires them in. Platform code (the macOS
hotkey, ``keyring``, ``fdesetup``) is imported lazily so the package imports on any OS.
"""

from sombra.privacy.blocked import DEFAULT_BLOCKED_APPS, DEFAULT_BLOCKED_TITLES, BlockedApps
from sombra.privacy.consent import (
    CONSENT_FILE,
    DEFAULT_NOTICE_PT_BR,
    ConsentRecord,
    ConsentRefusedError,
    require_consent,
)
from sombra.privacy.control import ControlServer, send_command
from sombra.privacy.diskcrypt import EncryptionState, EncryptionStatus, check_disk_encryption
from sombra.privacy.hotkey import DEFAULT_SHORTCUT, Shortcut, parse_shortcut
from sombra.privacy.pause import PauseController
from sombra.privacy.retention import (
    RetentionPolicy,
    SweepReport,
    UnsafePathError,
    delete_meeting,
    resolve_meeting,
    sweep,
)
from sombra.privacy.secrets import (
    KeyringBackend,
    MissingApiKeyError,
    SecretsError,
    clear_api_key,
    get_api_key,
    set_api_key,
)

__all__ = [
    "CONSENT_FILE",
    "DEFAULT_BLOCKED_APPS",
    "DEFAULT_BLOCKED_TITLES",
    "DEFAULT_NOTICE_PT_BR",
    "DEFAULT_SHORTCUT",
    "BlockedApps",
    "ConsentRecord",
    "ConsentRefusedError",
    "ControlServer",
    "EncryptionState",
    "EncryptionStatus",
    "KeyringBackend",
    "MissingApiKeyError",
    "PauseController",
    "RetentionPolicy",
    "SecretsError",
    "Shortcut",
    "SweepReport",
    "UnsafePathError",
    "check_disk_encryption",
    "clear_api_key",
    "delete_meeting",
    "get_api_key",
    "parse_shortcut",
    "require_consent",
    "resolve_meeting",
    "send_command",
    "set_api_key",
    "sweep",
]
