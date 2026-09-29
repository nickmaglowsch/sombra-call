import pytest
from audio_fakes import MAC_DEVICES, FakeSounddevice

from sombra.audio.devices import (
    TAP_ID,
    default_input,
    find_device,
    find_loopback,
    input_devices,
    list_audio_devices,
    macos_version_tuple,
    tap_supported,
)
from sombra.contracts import AudioDevice

RAW = FakeSounddevice(MAC_DEVICES).query_devices()


@pytest.mark.parametrize(
    ("release", "ok"),
    [
        ("14.4", True),
        ("14.4.1", True),
        ("15.0", True),
        ("14.3.1", False),
        ("13.6", False),
        ("", False),
        ("10.x", False),
    ],
)
def test_tap_supported(release: str, ok: bool) -> None:
    assert tap_supported(release) is ok


def test_version_tuple() -> None:
    assert macos_version_tuple("14.4.1") == (14, 4, 1)
    assert macos_version_tuple("") == ()


def test_duplicate_names_get_stable_suffixes() -> None:
    ids = [d.id for d in input_devices(RAW)]
    assert ids == ["MacBook Pro Microphone", "BlackHole 2ch", "USB Mic", "USB Mic#2"]
    usb2 = find_device(RAW, "USB Mic#2")
    assert (usb2.index, usb2.channels, usb2.sample_rate) == (4, 1, 16_000)


def test_list_devices_splits_mics_and_system_sources() -> None:
    assert list_audio_devices(RAW, tap_available=True) == [
        AudioDevice("MacBook Pro Microphone", "MacBook Pro Microphone", is_input=True),
        AudioDevice("USB Mic", "USB Mic", is_input=True),
        AudioDevice("USB Mic#2", "USB Mic", is_input=True),
        AudioDevice(TAP_ID, "System audio (Core Audio process tap)", is_input=False),
        AudioDevice("BlackHole 2ch", "BlackHole 2ch", is_input=False),
    ]
    assert TAP_ID not in [d.id for d in list_audio_devices(RAW, tap_available=False)]


def test_find_device_errors_list_known_ids() -> None:
    with pytest.raises(LookupError, match="'USB Mic#2'"):
        find_device(RAW, "Nope")


def test_loopback_and_default_input() -> None:
    lb = find_loopback(RAW)
    assert lb is not None
    assert lb.id == "BlackHole 2ch"
    assert find_loopback(RAW[:2]) is None
    assert default_input(RAW, 3).id == "USB Mic"
    assert default_input(RAW, 99).id == "MacBook Pro Microphone"  # unknown -> first mic
    with pytest.raises(LookupError):
        default_input(RAW[1:3], 0)
