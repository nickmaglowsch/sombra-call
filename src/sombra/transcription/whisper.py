"""whisper.cpp through ``pywhispercpp`` (see docs/adr/0002-whisper-cpp-binding.md).

One ``PyWhisperCppEngine`` holds one whisper context; it is not safe to share between
threads, so the transcriber builds one per channel. ``whisper_full`` releases the GIL,
so two engines really run in parallel (Metal on Apple Silicon, CPU elsewhere).

Loading is the exception: see ``_MODEL_LOAD``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sombra.transcription.vad import Pcm

log = logging.getLogger(__name__)

# pywhispercpp loads a model inside ``utils.redirect_stderr``, which ``dup``s and ``dup2``s
# the process-wide fd 2 to silence whisper.cpp. The transcriber builds one engine per
# channel on two threads at once; two overlapping redirects restore each other's saved fd
# and leave fd 2 closed or pointing at the wrong file (EBADF, #48). Loads take this lock.
# ``transcribe`` does not redirect, so inference still runs in parallel.
_MODEL_LOAD = threading.Lock()


class SpeechToText(Protocol):
    """Transcribe one closed utterance (16 kHz mono float32) to text."""

    def transcribe(self, audio: Pcm) -> str: ...


@dataclass(frozen=True, slots=True)
class WhisperSettings:
    model_path: Path
    language: str = "pt"  # ISO code, or "auto" to detect per utterance
    initial_prompt: str = ""  # see filters.build_initial_prompt (T4)
    n_threads: int = 4
    use_gpu: bool = True  # Metal on macOS; ignored by CPU-only builds


class PyWhisperCppEngine:
    def __init__(self, settings: WhisperSettings, *, model_cls: Any = None) -> None:
        if not settings.model_path.is_file():
            raise FileNotFoundError(
                f"whisper model not found at {settings.model_path}; run scripts/download_models.py"
            )
        if model_cls is None:
            from pywhispercpp.model import Model  # lazy: native binding

            model_cls = Model
        self.settings = settings
        params: dict[str, Any] = {
            "language": settings.language,
            "n_threads": settings.n_threads,
            "no_context": True,  # every utterance stands alone; stops loops carrying over
            "print_progress": False,
            "print_realtime": False,
            "print_timestamps": False,
            "suppress_blank": True,
            "suppress_nst": True,  # no "[Música]"-style non-speech tokens
        }
        if settings.initial_prompt:
            params["initial_prompt"] = settings.initial_prompt
        with _MODEL_LOAD:
            self._model = model_cls(
                str(settings.model_path),
                redirect_whispercpp_logs_to=None,
                context_params={"use_gpu": settings.use_gpu},
                **params,
            )

    def transcribe(self, audio: Pcm) -> str:
        segments = self._model.transcribe(audio)
        return " ".join(str(s.text).strip() for s in segments).strip()
