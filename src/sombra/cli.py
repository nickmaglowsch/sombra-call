"""Command-line entry point. Subcommands are added by the modules that own them."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from sombra import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sombra", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sombra {__version__}")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0
