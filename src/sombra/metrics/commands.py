"""``sombra report [<meeting>...]``: the success-metric table for one or more meetings."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "report",
        help="print the success metrics of one or more meetings",
        description="Success metrics per meeting, their aggregate and the L3 readiness line.",
    )
    p.add_argument(
        "meetings",
        nargs="*",
        type=Path,
        metavar="<meeting>",
        help="meeting folder(s); default: the current directory",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", dest="fmt", action="store_const", const="json", help="JSON output")
    fmt.add_argument(
        "--markdown",
        dest="fmt",
        action="store_const",
        const="markdown",
        help="Markdown output, to paste into a PR or doc",
    )
    p.add_argument(
        "--prices",
        type=Path,
        metavar="<file.toml>",
        help="price table, US$ per million tokens (docs/metrics.md); without it cost is unknown",
    )
    p.add_argument(
        "--missed",
        action="store_true",
        help="also list OUTROS lines naming the user that produced no trigger",
    )
    p.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="<name>",
        help="user alias for --missed (repeatable); default: aliases seen in logged triggers",
    )
    p.set_defaults(func=_run, fmt="text")


def _run(args: argparse.Namespace) -> int:
    from sombra.metrics.analysis import aggregate, analyze
    from sombra.metrics.missed import missed_triggers
    from sombra.metrics.prices import load_price_table
    from sombra.metrics.render import render

    meetings: list[Path] = [m.resolve() for m in args.meetings or [Path.cwd()]]
    missing = [m for m in meetings if not m.is_dir()]
    if missing:
        sys.stderr.write(f"sombra report: not a meeting folder: {missing[0]}\n")
        return 2
    prices = None
    if args.prices is not None:
        try:
            prices = load_price_table(args.prices)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"sombra report: bad price table {args.prices}: {exc}\n")
            return 2

    reports = [analyze(m, prices) for m in meetings]
    missed = None
    if args.missed:
        missed = {
            r.name: missed_triggers(m, args.alias) for r, m in zip(reports, meetings, strict=True)
        }
    sys.stdout.write(render(reports, aggregate(reports), args.fmt, missed) + "\n")
    return 0
