"""Model cache: pinned URLs, SHA-256 verification, atomic placement, download script."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import pytest

from sombra.transcription import models
from sombra.transcription.models import (
    SILERO_VAD,
    WHISPER_MODELS,
    ModelChecksumError,
    ModelFile,
    default_models_dir,
    ensure_model,
    whisper_model,
)

PAYLOAD = b"ggml" * 300_000  # > 1 MiB so the copy loop runs more than once
GOOD = ModelFile("ggml-fake.bin", "https://example.test/ggml-fake.bin", "", len(PAYLOAD))
GOOD = ModelFile(GOOD.filename, GOOD.url, hashlib.sha256(PAYLOAD).hexdigest(), GOOD.size)


class Opener:
    def __init__(self, payload: bytes = PAYLOAD) -> None:
        self.payload = payload
        self.urls: list[str] = []

    @contextmanager
    def __call__(self, url: str) -> Iterator[IO[bytes]]:
        self.urls.append(url)
        yield io.BytesIO(self.payload)


def test_downloads_verifies_and_caches(tmp_path: Path) -> None:
    opener = Opener()
    seen: list[tuple[int, int]] = []
    path = ensure_model(
        GOOD, tmp_path / "m", opener=opener, progress=lambda d, t: seen.append((d, t))
    )
    assert path == tmp_path / "m" / "ggml-fake.bin"
    assert path.read_bytes() == PAYLOAD
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD)) and len(seen) >= 2
    # second call is a cache hit: no network
    assert ensure_model(GOOD, tmp_path / "m", opener=opener) == path
    assert opener.urls == [GOOD.url]


def test_checksum_mismatch_leaves_nothing_behind(tmp_path: Path) -> None:
    with pytest.raises(ModelChecksumError) as err:
        ensure_model(GOOD, tmp_path, opener=Opener(b"tampered"))
    assert err.value.expected == GOOD.sha256
    assert err.value.actual == hashlib.sha256(b"tampered").hexdigest()
    assert list(tmp_path.iterdir()) == []


def test_interrupted_download_leaves_nothing_behind(tmp_path: Path) -> None:
    class Dies(io.BytesIO):
        def read(self, n: int | None = -1) -> bytes:
            raise ConnectionResetError("cut")

    @contextmanager
    def opener(url: str) -> Iterator[IO[bytes]]:
        yield Dies()

    with pytest.raises(ConnectionResetError):
        ensure_model(GOOD, tmp_path, opener=opener)
    assert list(tmp_path.iterdir()) == []


def test_only_https_urls_are_opened() -> None:
    with pytest.raises(ValueError, match="non-https"):
        models._urlopen("http://example.test/model.bin")


def test_registry_is_pinned() -> None:
    for m in [*WHISPER_MODELS.values(), SILERO_VAD]:
        assert m.url.startswith("https://"), m
        assert re.fullmatch(r"[0-9a-f]{64}", m.sha256), m
        assert m.size > 0, m
        assert "/main/" not in m.url and "/master/" not in m.url, f"unpinned revision: {m.url}"
    assert models.DEFAULT_WHISPER_MODEL == "large-v3-turbo-q5_0"
    assert {"tiny", "small-q5_1", "medium-q5_0"} <= set(WHISPER_MODELS)


def test_model_lookup_and_default_dir() -> None:
    assert whisper_model("tiny").filename == "ggml-tiny.bin"
    with pytest.raises(ValueError, match="known:"):
        whisper_model("nope")
    assert default_models_dir() == Path.home() / ".cache" / "sombra" / "models"


def _script() -> object:
    path = Path(__file__).resolve().parents[2] / "scripts" / "download_models.py"
    spec = importlib.util.spec_from_file_location("sombra_download_models", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_download_script_lists_and_validates(capsys: pytest.CaptureFixture[str]) -> None:
    script = _script()
    assert script.main(["--list"]) == 0  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert "large-v3-turbo-q5_0" in out and "silero-vad" in out
    assert script.main(["nope"]) == 2  # type: ignore[attr-defined]


def test_download_script_downloads_into_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script()
    calls: list[tuple[str, Path | None]] = []

    def fake_ensure(model: ModelFile, models_dir: Path | None = None) -> Path:
        calls.append((model.filename, models_dir))
        if model.filename == "ggml-small-q5_1.bin":
            raise ModelChecksumError(model.filename, "a", "b")
        return (models_dir or tmp_path) / model.filename

    monkeypatch.setattr(script, "ensure_model", fake_ensure)
    assert script.main(["tiny", "--dir", str(tmp_path)]) == 0  # type: ignore[attr-defined]
    assert calls == [("ggml-tiny.bin", tmp_path), ("silero_vad.onnx", tmp_path)]
    assert script.main(["small-q5_1"]) == 1  # type: ignore[attr-defined]
    assert "SHA-256 mismatch" in capsys.readouterr().err
