"""``sombra devices``: list microphones and system-audio sources with their config ids (A2)."""

from __future__ import annotations

import argparse
import json
import sys


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "devices", help="list audio inputs and system-audio sources (ids usable in config)"
    )
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.set_defaults(func=run_devices)


def run_devices(args: argparse.Namespace) -> int:
    from sombra.audio import default_source

    try:
        devices = default_source().list_devices()
    except (NotImplementedError, OSError, ImportError) as e:
        sys.stderr.write(f"sombra devices: {e}\n")
        return 2
    if args.json:
        rows = [{"id": d.id, "name": d.name, "is_input": d.is_input} for d in devices]
        sys.stdout.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        return 0
    sys.stdout.write("Microphones (audio.mic):\n")
    for d in devices:
        if d.is_input:
            sys.stdout.write(f"  {d.id}\n")
    sys.stdout.write("System audio (audio.system):\n")
    for d in devices:
        if not d.is_input:
            label = d.id if d.id == d.name else f"{d.id}  ({d.name})"
            sys.stdout.write(f"  {label}\n")
    return 0
