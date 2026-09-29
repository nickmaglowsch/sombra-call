import pytest

from sombra.privacy.hotkey import DEFAULT_SHORTCUT, Shortcut, parse_shortcut

CTRL, ALT, CMD, SHIFT = 1 << 18, 1 << 19, 1 << 20, 1 << 17


def test_default_is_ctrl_alt_cmd_p() -> None:
    assert parse_shortcut(DEFAULT_SHORTCUT) == Shortcut(frozenset({"ctrl", "alt", "cmd"}), "p")


@pytest.mark.parametrize("text", ["⌃⌥⌘P", "Control+Option+Command+P", " ctrl + alt + cmd + p "])
def test_equivalent_spellings(text: str) -> None:
    assert parse_shortcut(text) == parse_shortcut(DEFAULT_SHORTCUT)


@pytest.mark.parametrize("text", ["p", "ctrl+", "ctrl+pp", "hyper+p", "ctrl+ ", "Xp"])
def test_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_shortcut(text)


def test_matches_ns_event_flags() -> None:
    s = parse_shortcut(DEFAULT_SHORTCUT)
    assert s.ns_modifier_flags == CTRL | ALT | CMD
    caps_lock = 1 << 16
    device_bits = 0x0000_0101
    assert s.matches("p", CTRL | ALT | CMD)
    assert s.matches("P", CTRL | ALT | CMD | caps_lock | device_bits)
    assert not s.matches("p", CTRL | ALT)
    assert not s.matches("p", CTRL | ALT | CMD | SHIFT)
    assert not s.matches("o", CTRL | ALT | CMD)
