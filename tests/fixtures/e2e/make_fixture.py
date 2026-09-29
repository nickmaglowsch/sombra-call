"""Regenerate the ``sombra replay`` e2e fixture: synthetic PT-BR speech and a drawn slide.

No real voices or meetings: speech is eSpeak NG, the slide is drawn with Pillow. Run
from the repo root::

    uv run --with espeakng-loader python tests/fixtures/e2e/make_fixture.py

Writes (16 kHz mono s16le WAVs, total well under 1 MB)::

    others.wav   1.0 s silence, OTHERS_1, 1.5 s, QUESTIONS[0], 1.5 s, QUESTIONS[1], 1.5 s
    me.wav       1.0 s silence, ME_1, 1.0 s silence
    frames/1790703000000.png + .json   the slide (2026-09-29 17:30:00 UTC), shown first
    frames/1790703002000.png + .json   the same slide 2 s later (dedupe drops it)

Whisper ``tiny`` on eSpeak is rough (CI heard "Mariana, o que você acha" as "Maria da
Boquivoca, Acha"), so the user is called twice, with two phrasings and slower speech.
Either line is enough for a trigger; the detector's cooldown (G5) lets only one fire.
"""

from __future__ import annotations

import ctypes
import json
import wave
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SR = 16_000
WPM = 115  # eSpeak words per minute; slower than the default for whisper tiny
USER = "Maria"
OTHERS_1 = "Vou compartilhar o gráfico de vendas do trimestre."
QUESTIONS = (
    f"{USER}, você pode explicar este gráfico na tela?",
    f"{USER}, o que você acha desse gráfico aqui?",
)
ME_1 = "Bom dia a todos."
FRAME_MS = (1_790_703_000_000, 1_790_703_002_000)
SIDECAR = {"app": "zoom.us", "window_title": "Zoom - Vendas Q3"}

_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p
)


def _synth(text: str) -> np.ndarray:
    import espeakng_loader  # type: ignore[import-not-found]  # dev-only helper

    lib = ctypes.CDLL(espeakng_loader.get_library_path())
    data = espeakng_loader.get_data_path().encode()
    rate = lib.espeak_Initialize(2, 0, data, 0)  # AUDIO_OUTPUT_SYNCHRONOUS
    samples: list[int] = []

    def on_audio(wav: Any, n: int, _events: object) -> int:
        if wav and n > 0:
            samples.extend(wav[:n])
        return 0

    cb = _CALLBACK(on_audio)
    lib.espeak_SetSynthCallback(cb)
    lib.espeak_SetVoiceByName(b"pt-br")
    lib.espeak_SetParameter(1, WPM, 0)  # espeakRATE
    raw = text.encode()
    lib.espeak_Synth(raw, len(raw) + 1, 0, 1, 0, 1, None, None)  # POS_CHARACTER, CHARS_UTF8
    lib.espeak_Synchronize()
    pcm = np.asarray(samples, dtype=np.float32) / 32768.0
    t_out = np.arange(int(len(pcm) * SR / rate)) / SR
    return np.interp(t_out, np.arange(len(pcm)) / rate, pcm).astype(np.float32)


def _write_wav(path: Path, parts: list[np.ndarray]) -> None:
    audio = np.concatenate(parts)
    audio = audio / max(1e-9, float(np.abs(audio).max())) * 0.7
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((audio * 32767).astype("<i2").tobytes())


def _slide() -> Image.Image:
    img = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1280, 90), fill=(30, 60, 120))
    d.text((40, 30), "Vendas por trimestre", fill="white")
    d.line((120, 640, 1200, 640), fill="black", width=4)
    d.line((120, 640, 120, 140), fill="black", width=4)
    for i, h in enumerate((180, 260, 330, 470)):
        x = 220 + i * 240
        d.rectangle((x, 640 - h, x + 140, 640), fill=(60 + 40 * i, 120, 200 - 30 * i))
        d.text((x + 50, 655), f"Q{i + 1}", fill="black")
    return img


def main() -> None:
    rng = np.random.default_rng(17)

    def silence(seconds: float) -> np.ndarray:
        return (rng.standard_normal(int(seconds * SR)) * 1e-4).astype(np.float32)

    _write_wav(
        HERE / "others.wav",
        [
            silence(1.0),
            _synth(OTHERS_1),
            silence(1.5),
            _synth(QUESTIONS[0]),
            silence(1.5),
            _synth(QUESTIONS[1]),
            silence(1.5),
        ],
    )
    _write_wav(HERE / "me.wav", [silence(1.0), _synth(ME_1), silence(1.0)])

    frames = HERE / "frames"
    frames.mkdir(exist_ok=True)
    slide = _slide()
    for ms in FRAME_MS:
        slide.save(frames / f"{ms}.png", optimize=True)
        (frames / f"{ms}.json").write_text(json.dumps(SIDECAR) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
