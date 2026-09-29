"""Command-line entry point.

Subcommands are discovered, not listed here: any package ``sombra.<pkg>`` that has a
``commands`` module exposing ``register(subparsers)`` gets its commands added. So
modules add CLI commands without editing this file (and without merge conflicts).

``register`` adds one or more parsers and sets ``func`` on each; ``func(args)`` runs
the command and returns the exit code::

    def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
        p = subparsers.add_parser("report", help="print the meeting metrics")
        p.add_argument("meeting")
        p.set_defaults(func=_run)

Keep ``commands`` modules cheap to import: import heavy or platform-specific code
inside ``func``.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import pkgutil
from collections.abc import Sequence

import sombra
from sombra import __version__


def discover_command_modules(package: str = "sombra") -> list[str]:
    """Names of ``<package>.<sub>.commands`` modules that exist, sorted."""
    pkg = importlib.import_module(package)
    found = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if not info.ispkg:
            continue
        name = f"{package}.{info.name}.commands"
        if importlib.util.find_spec(name) is not None:
            found.append(name)
    return sorted(found)


def build_parser(package: str = sombra.__name__) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sombra", description="Sombra meeting agent.")
    parser.add_argument("--version", action="version", version=f"sombra {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name in discover_command_modules(package):
        importlib.import_module(name).register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0
    code: int = func(args)
    return code
