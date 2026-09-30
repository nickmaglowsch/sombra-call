"""``sombra models download|list|path`` with a fake downloader (no network)."""

import functools
import hashlib
import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import pytest

from sombra.cli import main
from sombra.transcription import commands
from sombra.transcription.models import SILERO_VAD, ModelFile, ensure_model

BODY = b"fake whisper weights" * 1000
SMALL = ModelFile(
    "ggml-fake.bin",
    "https://example.test/ggml-fake.bin",
    hashlib.sha256(BODY).hexdigest(),
    len(BODY),
)
VAD_BODY = b"fake vad"
VAD = ModelFile(
    "silero_vad.onnx", "https://example.test/vad", hashlib.sha256(VAD_BODY).hexdigest(), 8
)


@pytest.fixture
def fake_models(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route downloads to in-memory bodies; returns the URLs fetched."""
    fetched: list[str] = []
    bodies = {SMALL.url: BODY, VAD.url: VAD_BODY}

    @contextmanager
    def opener(url: str) -> Iterator[IO[bytes]]:
        fetched.append(url)
        yield io.BytesIO(bodies[url])

    def whisper(name: str) -> ModelFile:
        if name not in ("fake", "large-v3-turbo-q5_0"):
            raise ValueError(f"unknown whisper model {name!r}")
        return SMALL

    monkeypatch.setattr(commands, "whisper_model", whisper)
    monkeypatch.setattr(commands, "SILERO_VAD", VAD)
    monkeypatch.setattr(commands, "ensure_model", functools.partial(ensure_model, opener=opener))
    return fetched


def test_download_default_fetches_whisper_and_vad(
    tmp_path: Path, fake_models: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["models", "download", "--dir", str(tmp_path)]) == 0
    assert fake_models == [SMALL.url, VAD.url]
    assert (tmp_path / SMALL.filename).read_bytes() == BODY
    out, err = capsys.readouterr()
    assert f"ok  {tmp_path / SMALL.filename}" in out
    assert "100%" in err


def test_download_is_idempotent(
    tmp_path: Path, fake_models: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["models", "download", "fake", "--dir", str(tmp_path), "--quiet"]) == 0
    assert main(["models", "download", "fake", "--dir", str(tmp_path), "--quiet"]) == 0
    assert fake_models == [SMALL.url, VAD.url]  # second run downloads nothing
    assert capsys.readouterr().err == ""


def test_download_checksum_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @contextmanager
    def tampered(url: str) -> Iterator[IO[bytes]]:
        yield io.BytesIO(b"tampered")

    monkeypatch.setattr(commands, "whisper_model", lambda name: SMALL)
    monkeypatch.setattr(commands, "ensure_model", functools.partial(ensure_model, opener=tampered))
    assert main(["models", "download", "--dir", str(tmp_path), "--quiet"]) == 1
    assert "SHA-256 mismatch" in capsys.readouterr().err
    assert not (tmp_path / SMALL.filename).exists()


def test_download_network_error_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def offline(url: str) -> IO[bytes]:
        raise OSError("network is unreachable")

    monkeypatch.setattr(commands, "whisper_model", lambda name: SMALL)
    monkeypatch.setattr(commands, "ensure_model", functools.partial(ensure_model, opener=offline))
    assert main(["models", "download", "--dir", str(tmp_path), "--quiet"]) == 1
    assert "network is unreachable" in capsys.readouterr().err


def test_download_unknown_model(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["models", "download", "nope", "--dir", str(tmp_path)]) == 2
    assert "unknown whisper model 'nope'" in capsys.readouterr().err


def test_list_marks_present_and_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / SILERO_VAD.filename).write_bytes(b"x")
    assert main(["models", "list", "--dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    vad = next(line for line in lines if line.startswith("silero-vad"))
    turbo = next(line for line in lines if line.startswith("large-v3-turbo-q5_0"))
    tiny = next(line for line in lines if line.startswith("tiny"))
    assert "present" in vad
    assert "missing" in turbo and "(default)" in turbo
    assert "missing" in tiny and "(default)" not in tiny


def test_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["models", "path", "--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == f"{tmp_path}\n"
    assert main(["models", "path", "tiny", "--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == f"{tmp_path / 'ggml-tiny.bin'}\n"
    assert main(["models", "path", "silero-vad", "--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out == f"{tmp_path / 'silero_vad.onnx'}\n"
    assert main(["models", "path", "nope"]) == 2


def test_path_default_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["models", "path"]) == 0
    assert capsys.readouterr().out == f"{tmp_path / '.cache' / 'sombra' / 'models'}\n"


def test_models_requires_an_action() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["models"])
    assert exc.value.code == 2
