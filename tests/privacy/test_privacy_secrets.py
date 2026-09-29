import logging
import traceback

import pytest

from sombra.privacy import secrets
from sombra.privacy.secrets import (
    SERVICE,
    MissingApiKeyError,
    SecretsError,
    clear_api_key,
    get_api_key,
    has_api_key,
    set_api_key,
)

KEY = "sk-ant-TEST-super-secret-0123456789"


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.store[(service_name, username)]


class LeakyKeyring(FakeKeyring):
    """A backend whose errors echo the secret, like some real ones do."""

    def get_password(self, service_name: str, username: str) -> str | None:
        raise RuntimeError(f"dbus error while handling {KEY}")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise RuntimeError(f"cannot store {password}")


def test_set_get_clear_roundtrip() -> None:
    kr = FakeKeyring()
    set_api_key("Anthropic", f"  {KEY}\n", kr)
    assert kr.store == {(SERVICE, "anthropic"): KEY}
    assert get_api_key("anthropic", kr) == KEY
    assert has_api_key("anthropic", kr)
    assert clear_api_key("anthropic", kr) is True
    assert clear_api_key("anthropic", kr) is False
    assert not has_api_key("anthropic", kr)
    with pytest.raises(MissingApiKeyError, match="sombra auth set anthropic"):
        get_api_key("anthropic", kr)


@pytest.mark.parametrize("provider", ["", "../x", "a b", "x" * 33, "ANTHROPIC!"])
def test_bad_provider(provider: str) -> None:
    with pytest.raises(ValueError):
        set_api_key(provider, KEY, FakeKeyring())


def test_empty_key_rejected() -> None:
    with pytest.raises(ValueError):
        set_api_key("anthropic", "   ", FakeKeyring())


def _all_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)) + repr(exc) + str(exc.__context__)


def test_key_never_in_exceptions_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    kr = LeakyKeyring()
    errors: list[BaseException] = []
    for call in (
        lambda: set_api_key("anthropic", KEY, kr),
        lambda: get_api_key("anthropic", kr),
        lambda: clear_api_key("anthropic", kr),
    ):
        with pytest.raises(SecretsError) as info:
            call()
        errors.append(info.value)
    for exc in errors:
        assert exc.__cause__ is None
        assert exc.__context__ is None
        assert KEY not in _all_text(exc)
    assert KEY not in caplog.text


def test_default_backend_is_keyring_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    assert secrets.default_backend() is keyring
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "default_backend", lambda: fake)
    set_api_key("openai", KEY)
    assert get_api_key("openai") == KEY


@pytest.mark.hardware
def test_real_keychain_roundtrip() -> None:
    """Manual: stores a throwaway key in Keychain / Secret Service and removes it."""
    set_api_key("sombra-test", KEY)
    try:
        assert get_api_key("sombra-test") == KEY
    finally:
        assert clear_api_key("sombra-test")
