"""Streaming speech-to-text: Silero VAD + whisper.cpp, two channels, PT-BR first.

Owns PRD T1 (local streaming STT per channel, ~1 s after each utterance), T2 (model and
language choice, hallucination filtering) and T4 (meeting vocabulary as Whisper's
initial prompt). Implements ``sombra.contracts.Transcriber`` as ``WhisperTranscriber``.

Native bindings (onnxruntime, pywhispercpp) are imported lazily, so this package imports
on any OS without models present. Model files live in ``~/.cache/sombra/models`` and are
fetched by ``scripts/download_models.py``.
"""

from sombra.transcription.filters import HallucinationFilter, build_initial_prompt
from sombra.transcription.models import (
    DEFAULT_WHISPER_MODEL,
    SILERO_VAD,
    WHISPER_MODELS,
    ModelChecksumError,
    ModelFile,
    default_models_dir,
    ensure_model,
    whisper_model,
)
from sombra.transcription.transcriber import (
    SegmentStat,
    TranscriptionSettings,
    WhisperTranscriber,
)
from sombra.transcription.vad import Segment, Segmenter, SpeechDetector, VadSettings
from sombra.transcription.whisper import SpeechToText, WhisperSettings

__all__ = [
    "DEFAULT_WHISPER_MODEL",
    "SILERO_VAD",
    "WHISPER_MODELS",
    "HallucinationFilter",
    "ModelChecksumError",
    "ModelFile",
    "Segment",
    "SegmentStat",
    "Segmenter",
    "SpeechDetector",
    "SpeechToText",
    "TranscriptionSettings",
    "VadSettings",
    "WhisperSettings",
    "WhisperTranscriber",
    "build_initial_prompt",
    "default_models_dir",
    "ensure_model",
    "whisper_model",
]
