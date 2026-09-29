"""Regenerate ``ptbr_reuniao.wav``: synthetic PT-BR speech (eSpeak NG), no real voices.

Run from the repo root::

    uv run --with espeakng-loader python tests/fixtures/transcription/make_fixture.py

Layout (16 kHz mono s16le): 1.0 s silence, sentence 1, 1.5 s silence, sentence 2, 1.0 s silence.
"""

from __future__ import annotations

import ctypes
import wave
from pathlib import Path

import numpy as np

SENTENCES = (
    "Bom dia a todos. Vamos começar a reunião sobre o projeto.",
    "O prazo de entrega do relatório é na sexta-feira.",
)
OUT = Path(__file__).with_name("ptbr_reuniao.wav")
SR = 16_000

_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p
)


def _synth(text: str) -> np.ndarray:
    import espeakng_loader  # type: ignore[import-not-found]  # dev-only helper

    lib = ctypes.CDLL(espeakng_loader.get_library_path())
    data = espeakng_loader.get_data_path().encode()
    rate = lib.espeak_Initialize(2, 0, data, 0)  # AUDIO_OUTPUT_SYNCHRONOUS
    samples: list[int] = []

    def on_audio(wav: ctypes.Any, n: int, _events: ctypes.c_void_p) -> int:  # type: ignore[name-defined]
        if wav and n > 0:
            samples.extend(wav[:n])
        return 0

    cb = _CALLBACK(on_audio)
    lib.espeak_SetSynthCallback(cb)
    lib.espeak_SetVoiceByName(b"pt-br")
    lib.espeak_SetParameter(1, 140, 0)  # espeakRATE, words per minute
    raw = text.encode()
    lib.espeak_Synth(raw, len(raw) + 1, 0, 1, 0, 1, None, None)  # POS_CHARACTER, CHARS_UTF8
    lib.espeak_Synchronize()
    pcm = np.asarray(samples, dtype=np.float32) / 32768.0
    t_out = np.arange(int(len(pcm) * SR / rate)) / SR
    return np.interp(t_out, np.arange(len(pcm)) / rate, pcm).astype(np.float32)


def main() -> None:
    rng = np.random.default_rng(4)

    def silence(seconds: float) -> np.ndarray:
        return (rng.standard_normal(int(seconds * SR)) * 1e-4).astype(np.float32)

    parts = [silence(1.0), _synth(SENTENCES[0]), silence(1.5), _synth(SENTENCES[1]), silence(1.0)]
    audio = np.concatenate(parts)
    audio = audio / max(1e-9, float(np.abs(audio).max())) * 0.7
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((audio * 32767).astype("<i2").tobytes())


if __name__ == "__main__":
    main()
