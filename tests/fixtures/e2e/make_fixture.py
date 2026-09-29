"""Regenerate the ``sombra replay`` e2e fixture: synthetic PT-BR speech and a drawn slide.

No real voices or meetings: speech is eSpeak NG, the slide is drawn with Pillow. Run
from the repo root::

    uv run --with espeakng-loader python tests/fixtures/e2e/make_fixture.py

Writes (16 kHz mono s16le WAVs, total well under 1 MB)::

    others.wav   1.0 s silence, OTHERS_1, 1.5 s silence, QUESTION, 1.5 s silence
    me.wav       1.0 s silence, ME_1, 1.0 s silence
    frames/1790703000000.png + .json   the slide (2026-09-29 17:30:00 UTC), shown first
    frames/1790703002000.png + .json   the same slide 2 s later (dedupe drops it)
"""

from __future__ import annotations

import importlib.util
import json
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SR = 16_000
USER = "Mariana"
OTHERS_1 = "Vou compartilhar o gráfico de vendas do trimestre."
QUESTION = f"{USER}, o que você acha desse gráfico aqui?"
ME_1 = "Bom dia a todos."
FRAME_MS = (1_790_703_000_000, 1_790_703_002_000)
SIDECAR = {"app": "zoom.us", "window_title": "Zoom - Vendas Q3"}


def _synth(text: str) -> np.ndarray:
    # The eSpeak binding already written for the transcription fixture.
    path = HERE.parent / "transcription" / "make_fixture.py"
    spec = importlib.util.spec_from_file_location("_tx_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out: np.ndarray = module._synth(text)
    return out


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
        [silence(1.0), _synth(OTHERS_1), silence(1.5), _synth(QUESTION), silence(1.5)],
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
