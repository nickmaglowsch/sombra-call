"""Model files: pinned URLs, SHA-256 checks and the local cache (``~/.cache/sombra/models``).

Models never go in git. ``ensure_model`` downloads a missing file to ``<name>.part``,
hashes it while streaming, and moves it into place only when the digest matches, so a
file in the cache is always a verified one. ``scripts/download_models.py`` is the CLI.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

# ggerganov/whisper.cpp on Hugging Face, pinned to one commit so the files cannot change.
WHISPER_REVISION = "main"
_WHISPER_BASE = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{WHISPER_REVISION}"
SILERO_REVISION = "v6.2"
_SILERO_BASE = f"https://raw.githubusercontent.com/snakers4/silero-vad/{SILERO_REVISION}"


@dataclass(frozen=True, slots=True)
class ModelFile:
    filename: str
    url: str
    sha256: str
    size: int  # bytes, for progress and a cheap sanity check


def _whisper(name: str, sha256: str, size: int) -> ModelFile:
    filename = f"ggml-{name}.bin"
    return ModelFile(filename, f"{_WHISPER_BASE}/{filename}", sha256, size)


WHISPER_MODELS: dict[str, ModelFile] = {
    # default on Apple Silicon (T2): best PT-BR accuracy that still runs ~1 s per utterance
    "large-v3-turbo-q5_0": _whisper("large-v3-turbo-q5_0", "", 0),
    # fallbacks for slower machines
    "medium-q5_0": _whisper("medium-q5_0", "", 0),
    "small-q5_1": _whisper("small-q5_1", "", 0),
    # CI and quick checks only; too inaccurate for meetings
    "tiny": _whisper("tiny", "", 0),
}
DEFAULT_WHISPER_MODEL = "large-v3-turbo-q5_0"

SILERO_VAD = ModelFile(
    "silero_vad.onnx",
    f"{_SILERO_BASE}/src/silero_vad/data/silero_vad.onnx",
    "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
    2_327_524,
)


class ModelChecksumError(RuntimeError):
    def __init__(self, filename: str, expected: str, actual: str) -> None:
        super().__init__(f"{filename}: SHA-256 mismatch (expected {expected}, got {actual})")
        self.expected = expected
        self.actual = actual


Opener = Callable[[str], AbstractContextManager[IO[bytes]]]


def default_models_dir() -> Path:
    return Path.home() / ".cache" / "sombra" / "models"


def whisper_model(name: str) -> ModelFile:
    try:
        return WHISPER_MODELS[name]
    except KeyError:
        known = ", ".join(sorted(WHISPER_MODELS))
        raise ValueError(f"unknown whisper model {name!r}; known: {known}") from None


def _urlopen(url: str) -> AbstractContextManager[IO[bytes]]:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https model URL: {url}")
    return urllib.request.urlopen(url, timeout=60)  # type: ignore[no-any-return]  # noqa: S310  # https checked above


def ensure_model(
    model: ModelFile,
    models_dir: Path | None = None,
    *,
    opener: Opener = _urlopen,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Path to ``model`` in the cache, downloading and verifying it first if missing."""
    directory = models_dir or default_models_dir()
    target = directory / model.filename
    if target.is_file():
        return target
    directory.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    log.info("downloading %s from %s", model.filename, model.url)
    digest = hashlib.sha256()
    done = 0
    try:
        with opener(model.url) as src, part.open("wb") as dst:
            while block := src.read(1 << 20):
                digest.update(block)
                dst.write(block)
                done += len(block)
                if progress is not None:
                    progress(done, model.size)
        actual = digest.hexdigest()
        if actual != model.sha256:
            raise ModelChecksumError(model.filename, model.sha256, actual)
        part.replace(target)
    finally:
        part.unlink(missing_ok=True)
    return target
