from datetime import UTC, datetime

import pytest

from sombra.contracts import Screenshot
from sombra.privacy.blocked import BlockedApps, is_blocked


@pytest.mark.parametrize(
    ("app", "title"),
    [
        ("1Password", None),
        ("1Password 7", "Vault"),
        ("com.1password.1password", None),
        ("com.apple.mail", None),
        ("Mail", "Caixa de Entrada"),
        ("Keychain Access", None),
        ("ITAÚ", None),
        ("Itau", None),  # accent-insensitive
        ("Google Chrome", "Gmail - Caixa de entrada"),
        ("Safari", "Nubank | Internet Banking"),
        ("Arc", "Trocar senha - Conta"),
    ],
)
def test_default_blocks(app: str | None, title: str | None) -> None:
    assert is_blocked(app, title)


@pytest.mark.parametrize(
    ("app", "title"),
    [
        ("zoom.us", "Zoom Meeting"),
        ("Mailspring-ish", None),  # prefix without a separator is a different app
        ("Interface Builder", None),
        ("Google Chrome", "Roadmap Q4 - Google Slides"),
        (None, None),
        ("", ""),
    ],
)
def test_default_allows(app: str | None, title: str | None) -> None:
    assert not is_blocked(app, title)


def test_user_list_extends_defaults() -> None:
    b = BlockedApps.from_config(apps=["Slack"], titles=["Salário"])
    assert b.is_blocked("Slack", None)
    assert b.is_blocked("Excel", "planilha de SALARIO 2026")
    assert b.is_blocked("1Password", None)


def test_user_list_can_replace_defaults() -> None:
    b = BlockedApps.from_config(apps=["Slack"], extend_defaults=False)
    assert b.is_blocked("slack", None)
    assert not b.is_blocked("1Password", None)
    assert not b.is_blocked("Chrome", "Gmail")


def test_blank_entries_are_ignored() -> None:
    b = BlockedApps(apps=("", "  "), titles=("",))
    assert not b.is_blocked("anything", "any title")


def test_skip_hook_on_screenshot() -> None:
    b = BlockedApps()
    ts = datetime(2026, 9, 29, tzinfo=UTC)
    assert b.skip(Screenshot(ts=ts, image=b"", app="Bitwarden", window_title="x"))
    assert not b.skip(Screenshot(ts=ts, image=b"", app="zoom.us", window_title="Zoom"))
