"""API keys live only in the OS keychain (Keychain on macOS, Secret Service on Linux).

Keys are read and written through ``keyring``; nothing here ever writes a key to a
file, an environment variable or a log. Errors name the provider, never the key,
and backend exceptions are not chained (their message could echo the secret).
"""

from __future__ import annotations

import importlib
import re
from typing import Any, Protocol

SERVICE = "sombra"
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class SecretsError(RuntimeError):
    """The keychain could not be read or written. The message never contains a key."""


class MissingApiKeyError(SecretsError):
    pass


class KeyringBackend(Protocol):
    """The subset of the ``keyring`` module API we use; injectable in tests."""

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


def default_backend() -> KeyringBackend:
    """The ``keyring`` module, imported lazily (its Secret Service backend pulls D-Bus)."""
    module: Any = importlib.import_module("keyring")
    backend: KeyringBackend = module
    return backend


def _check_provider(provider: str) -> str:
    name = provider.strip().lower()
    if not _PROVIDER_RE.match(name):
        raise ValueError("provider must be 1-32 chars of a-z, 0-9, '-' or '_'")
    return name


def get_api_key(provider: str, backend: KeyringBackend | None = None) -> str:
    """The stored key for ``provider``; raises ``MissingApiKeyError`` if none is set."""
    name = _check_provider(provider)
    kr = backend or default_backend()
    failed = False
    key: str | None = None
    try:
        key = kr.get_password(SERVICE, name)
    except Exception:
        failed = True
    # Raised outside the except block so the backend error is not even the __context__.
    if failed:
        raise SecretsError(f"could not read the {name} API key from the OS keychain")
    if not key:
        raise MissingApiKeyError(f"no {name} API key stored; run `sombra auth set {name}`")
    return key


def has_api_key(provider: str, backend: KeyringBackend | None = None) -> bool:
    try:
        get_api_key(provider, backend)
    except MissingApiKeyError:
        return False
    return True


def set_api_key(provider: str, key: str, backend: KeyringBackend | None = None) -> None:
    name = _check_provider(provider)
    secret = key.strip()
    if not secret:
        raise ValueError("empty API key")
    kr = backend or default_backend()
    failed = False
    try:
        kr.set_password(SERVICE, name, secret)
    except Exception:
        failed = True
    if failed:
        raise SecretsError(f"could not store the {name} API key in the OS keychain")


def clear_api_key(provider: str, backend: KeyringBackend | None = None) -> bool:
    """Remove the stored key. Returns False if there was none."""
    name = _check_provider(provider)
    kr = backend or default_backend()
    failed = False
    existed = False
    try:
        existed = kr.get_password(SERVICE, name) is not None
        if existed:
            kr.delete_password(SERVICE, name)
    except Exception:
        failed = True
    if failed:
        raise SecretsError(f"could not remove the {name} API key from the OS keychain")
    return existed
