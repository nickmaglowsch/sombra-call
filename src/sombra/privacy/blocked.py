"""Blocked apps: frames from these apps or windows are never kept.

``BlockedApps.is_blocked`` is the frame pipeline's ``skip`` hook. The lists are
user-editable through the config (the orchestrator passes them in); the defaults
cover password managers, mail and banking apps.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from sombra.contracts import Screenshot

# Matched case-insensitively against the app name or bundle id, as a whole name or a
# dot/space-separated prefix ("1Password" also blocks "1Password 7", "com.1password.*").
DEFAULT_BLOCKED_APPS: tuple[str, ...] = (
    # password managers
    "1Password",
    "com.1password",
    "com.agilebits.onepassword",
    "Bitwarden",
    "com.bitwarden.desktop",
    "KeePassXC",
    "LastPass",
    "Dashlane",
    "Keychain Access",
    "com.apple.keychainaccess",
    "Passwords",
    "com.apple.Passwords",
    "Enpass",
    "Proton Pass",
    # mail
    "Mail",
    "com.apple.mail",
    "Microsoft Outlook",
    "com.microsoft.Outlook",
    "Spark",
    "Thunderbird",
    "Airmail",
    "Mimestream",
    # banking (Brazil first; edit the list in the config)
    "Nubank",
    "Itaú",
    "Bradesco",
    "Banco do Brasil",
    "Santander",
    "Caixa",
    "Inter",
    "C6 Bank",
    "PicPay",
    "Mercado Pago",
)

# Substrings of window titles (browser tabs) that mark sensitive pages.
DEFAULT_BLOCKED_TITLES: tuple[str, ...] = (
    "1Password",
    "Bitwarden",
    "LastPass",
    "Gmail",
    "Outlook",
    "Internet Banking",
    "Nubank",
    "Itaú",
    "Bradesco",
    "Banco do Brasil",
    "Santander",
    "Password",
    "Senha",
)


def _norm(s: str) -> str:
    folded = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in folded if not unicodedata.combining(c)).strip()


@dataclass(frozen=True)
class BlockedApps:
    apps: tuple[str, ...] = DEFAULT_BLOCKED_APPS
    titles: tuple[str, ...] = DEFAULT_BLOCKED_TITLES
    _apps: frozenset[str] = field(init=False, repr=False, compare=False)
    _titles: tuple[str, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_apps", frozenset(_norm(a) for a in self.apps if a.strip()))
        object.__setattr__(self, "_titles", tuple(_norm(t) for t in self.titles if t.strip()))

    @classmethod
    def from_config(
        cls,
        apps: Iterable[str] | None = None,
        titles: Iterable[str] | None = None,
        *,
        extend_defaults: bool = True,
    ) -> BlockedApps:
        """Build from user config lists, added to the defaults unless ``extend_defaults=False``."""
        base_apps = DEFAULT_BLOCKED_APPS if extend_defaults else ()
        base_titles = DEFAULT_BLOCKED_TITLES if extend_defaults else ()
        return cls(
            apps=(*base_apps, *(apps or ())),
            titles=(*base_titles, *(titles or ())),
        )

    def is_blocked(self, app: str | None, window_title: str | None) -> bool:
        if app:
            name = _norm(app)
            if name in self._apps:
                return True
            for blocked in self._apps:
                if name.startswith(blocked) and name[len(blocked)] in ". -":
                    return True
        if window_title:
            title = _norm(window_title)
            if any(t in title for t in self._titles):
                return True
        return False

    def skip(self, shot: Screenshot) -> bool:
        """Frame-pipeline hook: True means drop this screenshot before it touches disk."""
        return self.is_blocked(shot.app, shot.window_title)


_DEFAULT = BlockedApps()


def is_blocked(app: str | None, window_title: str | None) -> bool:
    """``is_blocked`` against the default lists."""
    return _DEFAULT.is_blocked(app, window_title)
