"""Sign ``Sombra.app`` inside-out with the hardened runtime (ADR 0050)::

    python packaging/macos/sign.py build/macos/dist/Sombra.app --identity -          # ad-hoc
    python packaging/macos/sign.py Sombra.app --identity "$IDENTITY" --timestamp      # release

Order matters: codesign seals what a bundle contains, so every nested Mach-O (dylibs,
Python extension modules, the CLI shim) is signed first, deepest first, then any nested
bundle (``.framework``/``.bundle``), then the app itself with the entitlements. Every
signature uses the hardened runtime (``--options runtime``), which notarization requires.
Ad-hoc (``--identity -``) signs the same way without a secure timestamp: that is what PR
runs build and smoke-test, so the release path is the same code with a real identity.

It ends with ``codesign --verify --deep --strict``. Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sombra_app as app  # noqa: E402  # next to this file

# First four bytes of a Mach-O: 32/64-bit, either byte order, and universal ("fat").
MACHO_MAGICS = frozenset(
    bytes.fromhex(h) for h in ("feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe")
)
NESTED_BUNDLE_SUFFIXES = (".framework", ".bundle", ".app", ".appex", ".xpc")
BATCH = 64  # paths per codesign call: one process per file is slow for ~400 files

Runner = Callable[[Sequence[str]], None]


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as f:
        return f.read(4) in MACHO_MAGICS


def _depth(path: Path) -> int:
    return len(path.parts)


def signing_plan(bundle: Path) -> tuple[list[Path], list[Path]]:
    """``(mach-o files, nested bundles)`` to sign before the app, each deepest first.

    Symlinks are skipped (their targets are signed where they live). The app's own main
    executable and the CLI shim are left out: the shim gets its own identifier, and the
    main executable is signed when the bundle is.
    """
    main_exe = bundle / "Contents" / "MacOS" / app.EXECUTABLE
    shim = bundle / app.SHIM_RELPATH
    files = sorted(
        (p for p in bundle.rglob("*") if p not in {main_exe, shim} and is_macho(p)),
        key=lambda p: (-_depth(p), str(p)),
    )
    nested = sorted(
        (
            p
            for p in bundle.rglob("*")
            if p.is_dir() and not p.is_symlink() and p.suffix in NESTED_BUNDLE_SUFFIXES
        ),
        key=lambda p: (-_depth(p), str(p)),
    )
    return files, nested


def codesign_base(identity: str, *, timestamp: bool) -> list[str]:
    cmd = ["codesign", "--force", "--sign", identity, "--options", "runtime"]
    # A secure timestamp is what notarization needs; ad-hoc signatures cannot have one.
    cmd.append("--timestamp" if timestamp and identity != "-" else "--timestamp=none")
    return cmd


def commands(
    bundle: Path, identity: str, entitlements: Path, *, timestamp: bool
) -> list[list[str]]:
    """Every codesign call, in order, ending with the strict verification."""
    base = codesign_base(identity, timestamp=timestamp)
    files, nested = signing_plan(bundle)
    out: list[list[str]] = []
    for i in range(0, len(files), BATCH):
        out.append([*base, *(str(p) for p in files[i : i + BATCH])])
    out += [[*base, str(p)] for p in nested]
    shim = bundle / app.SHIM_RELPATH
    if shim.exists():
        out.append([*base, "--identifier", app.SHIM_ID, str(shim)])
    out.append([*base, "--entitlements", str(entitlements), str(bundle)])
    out.append(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)])
    return out


def _run(cmd: Sequence[str]) -> None:
    shown = cmd if len(cmd) < 12 else [*cmd[:8], f"... ({len(cmd) - 8} more)"]
    print("+", " ".join(shown), flush=True)
    subprocess.run(list(cmd), check=True)  # noqa: S603  # argv we built, no shell


def sign(
    bundle: Path,
    identity: str,
    entitlements: Path,
    *,
    timestamp: bool = False,
    run: Runner = _run,
) -> int:
    """Sign and verify; returns the number of codesign calls made."""
    if not (bundle / "Contents" / "Info.plist").is_file():
        raise FileNotFoundError(f"{bundle} is not an app bundle (no Contents/Info.plist)")
    cmds = commands(bundle, identity, entitlements, timestamp=timestamp)
    for cmd in cmds:
        run(cmd)
    return len(cmds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--identity", default="-", help='"-" for ad-hoc, or a SHA-1 / name')
    parser.add_argument("--entitlements", type=Path, default=HERE / "entitlements.plist")
    parser.add_argument("--timestamp", action="store_true", help="secure timestamp (release)")
    args = parser.parse_args(argv)
    files, nested = signing_plan(args.bundle)
    print(f"signing {len(files)} Mach-O files and {len(nested)} nested bundles", flush=True)
    sign(args.bundle, args.identity, args.entitlements, timestamp=args.timestamp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
