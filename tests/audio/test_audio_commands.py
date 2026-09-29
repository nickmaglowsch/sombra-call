import json
import sys

import pytest

import sombra.audio
from sombra.cli import main
from sombra.contracts import AudioDevice

DEVICES = [
    AudioDevice("MacBook Pro Microphone", "MacBook Pro Microphone", is_input=True),
    AudioDevice("tap:system", "System audio (Core Audio process tap)", is_input=False),
    AudioDevice("BlackHole 2ch", "BlackHole 2ch", is_input=False),
]


class _Src:
    def list_devices(self) -> list[AudioDevice]:
        return DEVICES


@pytest.fixture
def fake_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sombra.audio, "default_source", lambda **_: _Src())


def test_devices_table(fake_source: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["devices"]) == 0
    out = capsys.readouterr().out
    assert out == (
        "Microphones (audio.mic):\n"
        "  MacBook Pro Microphone\n"
        "System audio (audio.system):\n"
        "  tap:system  (System audio (Core Audio process tap))\n"
        "  BlackHole 2ch\n"
    )


def test_devices_json(fake_source: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["devices", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[1] == {"id": "tap:system", "name": DEVICES[1].name, "is_input": False}


def test_devices_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert main(["devices"]) == 2
    assert "not implemented on linux" in capsys.readouterr().err


def test_default_source_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    from sombra.audio.macos import MacAudioSource

    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(sombra.audio.default_source(system="BlackHole 2ch"), MacAudioSource)
