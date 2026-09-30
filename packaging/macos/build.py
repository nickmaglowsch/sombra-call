"""Build ``Sombra.app`` from a release wheel (ADR 0050)::

    python packaging/macos/build.py --wheel dist/sombra-0.2.0-py3-none-any.whl [--out build/macos]

1. Exports the locked runtime dependencies plus PyInstaller (``uv.lock``, group ``app``)
   and installs them, then the wheel, into a clean venv on a uv-managed CPython 3.12.
2. Runs PyInstaller on ``sombra.spec`` (onedir ``.app``, version from the wheel).
3. Adds the PT-BR/EN ``InfoPlist.strings`` and compiles the CLI shim into
   ``Contents/Helpers/sombra``.

The result is unsigned apart from PyInstaller's ad-hoc signatures; ``sign.py`` signs it.
It prints ``app=``, ``version=`` and ``size_bytes=`` lines (and appends them to
``--github-output``). On Linux it builds the onedir folder only, for quick local checks.

Standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import sombra_app as app  # noqa: E402  # next to this file

Runner = Callable[..., None]


def wheel_version(wheel: Path) -> str:
    """``sombra-0.2.0rc1-py3-none-any.whl`` -> ``0.2.0rc1``."""
    parts = wheel.name.split("-")
    if len(parts) != 5 or parts[0] != "sombra" or not wheel.name.endswith(".whl"):
        raise ValueError(f"not a sombra wheel: {wheel.name}")
    return parts[1]


def write_localizations(app_path: Path) -> list[Path]:
    """``Contents/Resources/<lang>.lproj/InfoPlist.strings``, one per language we ship."""
    written = []
    for lang in app.USAGE:
        out = app_path / "Contents" / "Resources" / f"{lang}.lproj" / "InfoPlist.strings"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(app.infoplist_strings(lang), encoding="utf-8")
        written.append(out)
    return written


def shim_command(cc: str, out: Path, *, darwin: bool) -> list[str]:
    cmd = [cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror"]
    if darwin:
        cmd += ["-arch", "arm64", f"-mmacosx-version-min={app.MIN_MACOS}"]
    cmd += ["-o", str(out), str(HERE / "shim" / "sombra.c")]
    if not darwin:
        cmd.append("-ldl")
    return cmd


def tree_size(path: Path) -> int:
    """Bytes of every regular file under ``path``, symlinks not followed."""
    return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def _run(cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(list(cmd), check=True, cwd=cwd, env=env)  # noqa: S603  # argv we built, no shell


def build(wheel: Path, out: Path, *, cc: str = "cc", run: Runner = _run) -> Path:
    version = wheel_version(wheel)
    uv = shutil.which("uv") or "uv"
    out = out.resolve()
    venv = out / "venv"
    work = out / "work"
    dist = out / "dist"
    shutil.rmtree(out, ignore_errors=True)
    work.mkdir(parents=True)
    env = {**os.environ, "UV_PYTHON_PREFERENCE": "only-managed", "SOMBRA_VERSION": version}

    requirements = work / "requirements.txt"
    run(
        [
            *(uv, "export", "--frozen", "--no-dev", "--group", "app", "--no-emit-project"),
            *("--format", "requirements-txt", "--output-file", str(requirements)),
        ],
        cwd=ROOT,
        env=env,
    )
    run([uv, "venv", "--python", "3.12", str(venv)], env=env)
    python = venv / "bin" / "python"
    run([uv, "pip", "install", "--python", str(python), "-r", str(requirements)], env=env)
    run([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel.resolve())], env=env)
    run(
        [
            *(str(venv / "bin" / "pyinstaller"), "--noconfirm", "--clean"),
            *("--distpath", str(dist), "--workpath", str(work / "pyinstaller")),
            str(HERE / "sombra.spec"),
        ],
        env=env,
    )

    darwin = sys.platform == "darwin"
    if not darwin:
        return dist / app.APP_NAME
    bundle = dist / app.APP_DIR
    write_localizations(bundle)
    shim = bundle / app.SHIM_RELPATH
    shim.parent.mkdir(parents=True, exist_ok=True)
    run(shim_command(cc, shim, darwin=True))
    return bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "macos")
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    built = build(args.wheel, args.out, cc=args.cc)
    lines = f"app={built}\nversion={wheel_version(args.wheel)}\nsize_bytes={tree_size(built)}\n"
    sys.stdout.write(lines)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
