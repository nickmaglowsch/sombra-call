"""Write and verify ``SHA256SUMS`` for the release assets.

The file uses the ``sha256sum`` / ``shasum -a 256`` format (``<hex>  <name>``), so users
can check a download with the stock tools (see docs/release.md)::

    python scripts/release/checksums.py write dist     # hash every file in dist/
    python scripts/release/checksums.py verify dist    # exit 1 on a mismatch or missing file

Standard library only: the release workflow runs it before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path

SUMS_NAME = "SHA256SUMS"
_CHUNK = 1 << 20


class ChecksumError(Exception):
    """A file is missing, altered, or the sums file is malformed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def assets(directory: Path) -> list[Path]:
    """The files that go in the sums file: every regular file except the sums file itself."""
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.name != SUMS_NAME and p.name[0] != "."
    )


def render(directory: Path) -> str:
    files = assets(directory)
    if not files:
        raise ChecksumError(f"no files to hash in {directory}")
    return "".join(f"{sha256_file(p)}  {p.name}\n" for p in files)


def write(directory: Path) -> Path:
    out = directory / SUMS_NAME
    out.write_text(render(directory), encoding="utf-8")
    return out


def parse(text: str) -> dict[str, str]:
    """``{file name: hex digest}`` from ``sha256sum`` output (text or ``*`` binary mode)."""
    sums: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        digest, sep, name = line.partition(" ")
        name = name.lstrip(" ").removeprefix("*")
        if not sep or len(digest) != 64 or not name:
            raise ChecksumError(f"{SUMS_NAME} line {n} is malformed: {raw!r}")
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ChecksumError(f"{SUMS_NAME} line {n} names a path, not a file: {name!r}")
        if name in sums:
            raise ChecksumError(f"{SUMS_NAME} lists {name!r} twice")
        sums[name] = digest.lower()
    if not sums:
        raise ChecksumError(f"{SUMS_NAME} is empty")
    return sums


def verify(directory: Path, *, require_all: bool = True) -> list[str]:
    """Check every listed file. With ``require_all``, an unlisted asset is also an error.

    Returns the verified names; raises ``ChecksumError`` listing every problem found.
    """
    sums_path = directory / SUMS_NAME
    if not sums_path.is_file():
        raise ChecksumError(f"{sums_path} not found")
    sums = parse(sums_path.read_text(encoding="utf-8"))
    problems = []
    for name, expected in sums.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
        elif sha256_file(path) != expected:
            problems.append(f"{name}: checksum mismatch")
    if require_all:
        problems += [f"{p.name}: not listed" for p in assets(directory) if p.name not in sums]
    if problems:
        raise ChecksumError("; ".join(problems))
    return sorted(sums)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["write", "verify"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "write":
            out = write(args.directory)
            sys.stdout.write(out.read_text(encoding="utf-8"))
        else:
            for name in verify(args.directory):
                sys.stdout.write(f"{name}: OK\n")
    except ChecksumError as e:
        sys.stderr.write(f"checksums: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
