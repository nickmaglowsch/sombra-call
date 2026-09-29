"""``sombra models download|list|path``: the local STT models (T1, T2).

A thin CLI over ``sombra.transcription.models``: files are pinned by URL and SHA-256,
downloaded to ``<name>.part`` and moved into place only when the digest matches.
``scripts/install.sh`` runs ``sombra models download`` for the default whisper model
and Silero VAD.
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
    ModelFile,
    default_models_dir,
    ensure_model,
    whisper_model,
)

SILERO_NAME = "silero-vad"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    models = subparsers.add_parser("models", help="download and list the local STT models")
    sub = models.add_subparsers(dest="models_command", metavar="<download|list|path>")
    sub.required = True

    p = sub.add_parser("download", help="download whisper model(s) + Silero VAD, verified")
    p.add_argument(
        "names",
        nargs="*",
        metavar="model",
        help=f"whisper models (default {DEFAULT_WHISPER_MODEL})",
    )
    _add_dir(p)
    p.add_argument("--quiet", action="store_true", help="no progress output")
    p.set_defaults(func=_download)

    p = sub.add_parser("list", help="list the known models and whether each is downloaded")
    _add_dir(p)
    p.set_defaults(func=_list)

    p = sub.add_parser("path", help="print the models folder, or one model's file path")
    p.add_argument("name", nargs="?", help="a whisper model name or 'silero-vad'")
    _add_dir(p)
    p.set_defaults(func=_path)


def _add_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dir", type=Path, default=None, help="models folder (default ~/.cache/sombra/models)"
    )


def _resolve(name: str) -> ModelFile:
    return SILERO_VAD if name == SILERO_NAME else whisper_model(name)


class _Progress:
    """Whole-percent progress on stderr, one line per file."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.last = -1

    def __call__(self, done: int, total: int) -> None:
        pct = min(100, done * 100 // total) if total else 100
        if pct != self.last:
            self.last = pct
            sys.stderr.write(f"\r  {self.filename}: {pct:3d}%")
            sys.stderr.flush()

    def end(self) -> None:
        if self.last >= 0:
            sys.stderr.write("\n")


def _download(args: argparse.Namespace) -> int:
    try:
        files = [whisper_model(n) for n in (args.names or [DEFAULT_WHISPER_MODEL])]
    except ValueError as e:
        sys.stderr.write(f"sombra models download: {e}\n")
        return 2
    files.append(SILERO_VAD)
    for f in files:
        progress = None if args.quiet else _Progress(f.filename)
        try:
            path = ensure_model(f, args.dir, progress=progress)
        except ModelChecksumError as e:
            sys.stderr.write(f"sombra models download: {e}\n")
            return 1
        except OSError as e:
            sys.stderr.write(f"sombra models download: {f.filename}: {e}\n")
            return 1
        finally:
            if progress is not None:
                progress.end()
        sys.stdout.write(f"ok  {path}\n")
    return 0


def _list(args: argparse.Namespace) -> int:
    directory = args.dir or default_models_dir()
    rows = [(n, m, n == DEFAULT_WHISPER_MODEL) for n, m in WHISPER_MODELS.items()]
    rows.append((SILERO_NAME, SILERO_VAD, True))
    for name, m, default in rows:
        state = "present" if (directory / m.filename).is_file() else "missing"
        tag = " (default)" if default else ""
        sys.stdout.write(f"{name:22} {m.size / 1e6:8.1f} MB  {state:7}  {m.filename}{tag}\n")
    return 0


def _path(args: argparse.Namespace) -> int:
    directory: Path = args.dir or default_models_dir()
    if args.name is None:
        sys.stdout.write(f"{directory}\n")
        return 0
    try:
        model = _resolve(args.name)
    except ValueError as e:
        sys.stderr.write(f"sombra models path: {e}\n")
        return 2
    sys.stdout.write(f"{directory / model.filename}\n")
    return 0
