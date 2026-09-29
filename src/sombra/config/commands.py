"""``sombra new`` and ``sombra profiles list``.

These live in ``config`` rather than ``store`` because they need both: ``config`` is
a wiring package (see tests/test_architecture.py) and may import ``store``, while
``store`` may import only ``sombra.contracts``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    new = subparsers.add_parser("new", help="create a meeting folder and print its path")
    new.add_argument("name", help="meeting name, e.g. 'Daily time X'")
    new.add_argument("--profile", help="profile from ~/.config/sombra/profiles/<name>.toml")
    new.add_argument(
        "--config", type=Path, help="user config file (default: ~/.config/sombra/config.toml)"
    )
    new.set_defaults(func=_new)

    profiles = subparsers.add_parser("profiles", help="manage meeting profiles")
    sub = profiles.add_subparsers(dest="profiles_command", metavar="<action>", required=True)
    ls = sub.add_parser("list", help="list the profiles in ~/.config/sombra/profiles/")
    ls.set_defaults(func=_profiles_list)


def _new(args: argparse.Namespace) -> int:
    from sombra.config.loader import load_profile, load_user_config
    from sombra.config.schema import ConfigError
    from sombra.store import create_meeting

    try:
        cfg = load_user_config(args.config)
        profile = load_profile(args.profile) if args.profile else None
        meeting_dir = create_meeting(
            cfg.meetings_root,
            args.name,
            profile.name if profile else None,
            context_paths=profile.context if profile else (),
            aliases=cfg.user.all_aliases,
            autonomy_level=cfg.autonomy_level,
            allowed_topics=profile.allowed_topics if profile else (),
        )
    except (ConfigError, FileNotFoundError, ValueError) as e:
        sys.stderr.write(f"sombra new: {e}\n")
        return 2
    sys.stdout.write(f"{meeting_dir}\n")
    return 0


def _profiles_list(args: argparse.Namespace) -> int:
    from sombra.config.loader import list_profiles, profiles_dir
    from sombra.config.schema import ConfigError

    directory = profiles_dir()
    try:
        profiles = list_profiles(directory)
    except ConfigError as e:
        sys.stderr.write(f"sombra profiles list: {e}\n")
        return 2
    if not profiles:
        sys.stderr.write(f"no profiles in {directory}\n")
        return 0
    for p in profiles:
        sys.stdout.write(f"{p.name}\t{p.description}\n" if p.description else f"{p.name}\n")
    return 0
