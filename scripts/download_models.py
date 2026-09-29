"""Download Sombra's local models into ~/.cache/sombra/models with SHA-256 checks.

uv run python scripts/download_models.py                 # default whisper + Silero VAD
uv run python scripts/download_models.py tiny small-q5_1  # specific whisper models
uv run python scripts/download_models.py --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sombra.transcription.models import (
    DEFAULT_WHISPER_MODEL,
    SILERO_VAD,
    WHISPER_MODELS,
    ModelChecksumError,
    default_models_dir,
    ensure_model,
    whisper_model,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "models", nargs="*", help=f"whisper models (default {DEFAULT_WHISPER_MODEL})"
    )
    parser.add_argument("--dir", type=Path, default=None, help=f"default {default_models_dir()}")
    parser.add_argument("--list", action="store_true", help="list known models and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, m in WHISPER_MODELS.items():
            print(f"{name:22} {m.size / 1e6:8.1f} MB  {m.url}")
        print(f"{'silero-vad':22} {SILERO_VAD.size / 1e6:8.1f} MB  {SILERO_VAD.url}")
        return 0

    try:
        files = [whisper_model(n) for n in (args.models or [DEFAULT_WHISPER_MODEL])]
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    files.append(SILERO_VAD)
    for f in files:
        try:
            path = ensure_model(f, args.dir)
        except ModelChecksumError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"ok  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
