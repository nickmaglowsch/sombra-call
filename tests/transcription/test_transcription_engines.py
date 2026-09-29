"""Native wrappers with the native part faked: pywhispercpp Model and the Silero ONNX session."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from sombra.transcription.silero import CONTEXT_SAMPLES, SileroVad
from sombra.transcription.vad import FRAME_SAMPLES
from sombra.transcription.whisper import PyWhisperCppEngine, WhisperSettings


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModel:
    instances: ClassVar[list[FakeModel]] = []

    def __init__(self, path: str, **kwargs: Any) -> None:
        self.path = path
        self.kwargs = kwargs
        self.audio: list[np.ndarray] = []
        FakeModel.instances.append(self)

    def transcribe(self, audio: np.ndarray) -> list[FakeSegment]:
        self.audio.append(audio)
        return [FakeSegment(" Nick, o que"), FakeSegment(" você acha? ")]


def test_whisper_engine_passes_settings_and_joins_segments(tmp_path: Path) -> None:
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"x")
    s = WhisperSettings(
        model, language="auto", initial_prompt="Reunião. Termos: Nick.", n_threads=2
    )
    engine = PyWhisperCppEngine(s, model_cls=FakeModel)
    fake = FakeModel.instances[-1]
    assert fake.path == str(model)
    assert fake.kwargs["language"] == "auto"
    assert fake.kwargs["initial_prompt"] == "Reunião. Termos: Nick."
    assert fake.kwargs["n_threads"] == 2
    assert fake.kwargs["no_context"] is True
    assert fake.kwargs["context_params"] == {"use_gpu": True}
    audio = np.zeros(16000, dtype=np.float32)
    assert engine.transcribe(audio) == "Nick, o que você acha?"
    assert fake.audio[0] is audio


def test_whisper_engine_without_prompt_omits_it(tmp_path: Path) -> None:
    model = tmp_path / "m.bin"
    model.write_bytes(b"x")
    PyWhisperCppEngine(WhisperSettings(model), model_cls=FakeModel)
    kwargs = FakeModel.instances[-1].kwargs
    assert "initial_prompt" not in kwargs and kwargs["language"] == "pt"


def test_whisper_engine_requires_the_model_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="download_models"):
        PyWhisperCppEngine(WhisperSettings(tmp_path / "missing.bin"), model_cls=FakeModel)


class StderrRedirectingModel:
    """Loads the way pywhispercpp does: silences whisper.cpp by swapping the process's fd 2."""

    guard = threading.Lock()
    active = 0
    peak = 0

    def __init__(self, path: str, **kwargs: Any) -> None:
        cls = StderrRedirectingModel
        with cls.guard:
            cls.active += 1
            cls.peak = max(cls.peak, cls.active)
        # pywhispercpp.utils.redirect_stderr(to=None), with a slow load in the middle
        with Path(os.devnull).open("w") as devnull:
            saved_fd = os.dup(2)
            os.dup2(devnull.fileno(), 2)
            try:
                time.sleep(0.05)
            finally:
                os.dup2(saved_fd, 2)
                os.close(saved_fd)
        with cls.guard:
            cls.active -= 1


def test_concurrent_engine_loads_leave_stderr_intact(tmp_path: Path) -> None:
    """#48: the transcriber loads one engine per channel on two threads at once."""
    model = tmp_path / "m.bin"
    model.write_bytes(b"x")
    before = os.fstat(2)
    errors: list[BaseException] = []

    def load() -> None:
        try:
            PyWhisperCppEngine(WhisperSettings(model), model_cls=StderrRedirectingModel)
        except BaseException as exc:  # surfaced below; a thread would swallow it
            errors.append(exc)

    threads = [threading.Thread(target=load) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert StderrRedirectingModel.peak == 1
    after = os.fstat(2)  # raises EBADF if a racing restore closed it
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_real_binding_imports() -> None:
    """The wheel we depend on exists for this OS and exposes the API we call."""
    from pywhispercpp.model import Model

    assert callable(Model.transcribe)


class FakeSession:
    def __init__(self) -> None:
        self.inputs: list[dict[str, np.ndarray]] = []

    def run(self, _outputs: None, feeds: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        self.inputs.append(feeds)
        return np.array([[0.75]], dtype=np.float32), feeds["state"] + 1


def test_silero_carries_context_and_state() -> None:
    session = FakeSession()
    vad = SileroVad(Path("unused"), session=session)
    a = np.linspace(0, 1, FRAME_SAMPLES, dtype=np.float32)
    b = np.zeros(FRAME_SAMPLES, dtype=np.float32)
    assert vad(a) == pytest.approx(0.75)
    vad(b)
    first, second = session.inputs
    assert first["input"].shape == (1, CONTEXT_SAMPLES + FRAME_SAMPLES)
    assert not first["input"][0, :CONTEXT_SAMPLES].any()
    assert np.array_equal(second["input"][0, :CONTEXT_SAMPLES], a[-CONTEXT_SAMPLES:])
    assert int(second["sr"]) == 16000
    assert second["state"].shape == (2, 1, 128) and second["state"].max() == 1
    vad.reset()
    vad(b)
    assert session.inputs[-1]["state"].max() == 0


def test_silero_rejects_wrong_frame_size() -> None:
    vad = SileroVad(Path("unused"), session=FakeSession())
    with pytest.raises(ValueError, match="512"):
        vad(np.zeros(100, dtype=np.float32))


def test_silero_requires_the_model_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="download_models"):
        SileroVad(tmp_path / "silero_vad.onnx")
