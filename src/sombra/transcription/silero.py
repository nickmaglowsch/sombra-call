"""Silero VAD (v6, ONNX) as a ``SpeechDetector``, run with onnxruntime on one CPU thread.

We call the ONNX model directly instead of the ``silero-vad`` package, which pulls in
PyTorch (~1 GB) for what is a 2 MB model. The wrapper mirrors ``OnnxWrapper`` from
silero-vad v6.2: 64 samples of context are prepended to every 512-sample frame and the
recurrent state is carried between calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from sombra.contracts import SAMPLE_RATE
from sombra.transcription.vad import FRAME_SAMPLES, Pcm

CONTEXT_SAMPLES = 64


class SileroVad:
    def __init__(self, model_path: Path, *, session: Any = None) -> None:
        if session is None:
            if not model_path.is_file():
                raise FileNotFoundError(
                    f"Silero VAD model not found at {model_path}; run scripts/download_models.py"
                )
            import onnxruntime  # lazy: native binding

            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            session = onnxruntime.InferenceSession(
                str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
        self._session = session
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def __call__(self, frame: Pcm) -> float:
        if frame.shape != (FRAME_SAMPLES,):
            raise ValueError(f"Silero VAD takes {FRAME_SAMPLES} samples, got {frame.shape}")
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, state = self._session.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._state = state
        self._context = x[:, -CONTEXT_SAMPLES:]
        return float(np.asarray(out).reshape(-1)[0])
