"""``sombra new``, ``profiles list``, ``config init``, ``doctor`` and ``setup``.

These live in ``config`` rather than ``store`` because they need both: ``config`` is
a wiring package (see tests/test_architecture.py) and may import ``store``, while
``store`` may import only ``sombra.contracts``. ``doctor`` is here for the same reason:
it inspects ``transcription``, ``privacy`` and ``audio`` (see ``config/doctor.py``).
``setup`` (``config/setup.py``) is the agent-provider wizard (#47).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Written by `sombra config init` (and so by scripts/install.sh). Every value is the
# default, so the file only makes the keys discoverable; keep in sync with docs/config.md.
DEFAULT_CONFIG_TOML = """\
# Sombra user config. Every key is optional; these are the defaults.
# Reference: docs/config.md. API keys never go here: use `sombra auth set <provider>`.

meetings_root = "~/Sombra/meetings"   # where `sombra new` creates meeting folders
autonomy_level = "L1"                 # L0 record, L1 suggest, L2 answer after approval
capture_interval_s = 5                # seconds between screenshots, 1-60

[user]
name = ""                             # your name; always one of the trigger aliases
aliases = []                          # other names people call you

[retention]
frames_days = 7
transcripts_days = 30

[audio]
# mic = "MacBook Pro Microphone"      # unset: system default input (see `sombra devices`)
# system = "BlackHole 2ch"            # unset: platform default loopback/tap

[models]
stt = "large-v3-turbo"                # whisper.cpp model
agent = "sonnet"                      # agent backend model alias
summary = "haiku"                     # rolling summary / minutes model alias

[brain]
backend = "claude-api"                # claude-code, claude-api or codex: run `sombra setup`

[summary]
backend = "follow"                    # the [brain] backend; or claude-code, claude-api, codex, none
"""


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

    config = subparsers.add_parser("config", help="manage the user config file")
    sub = config.add_subparsers(dest="config_command", metavar="<init|path>", required=True)
    p = sub.add_parser("init", help="write the default config.toml if there is none")
    p.add_argument("--config", type=Path, help="where (default: ~/.config/sombra/config.toml)")
    p.set_defaults(func=_config_init)
    p = sub.add_parser("path", help="print the user config file path")
    p.set_defaults(func=_config_path)

    doctor = subparsers.add_parser(
        "doctor", help="check that this machine is ready and say what is left to do"
    )
    doctor.add_argument("--json", action="store_true", help="print JSON instead of text")
    doctor.add_argument(
        "--config", type=Path, help="user config file (default: ~/.config/sombra/config.toml)"
    )
    doctor.add_argument(
        "--models-dir", type=Path, help="models folder (default: ~/.cache/sombra/models)"
    )
    doctor.add_argument(
        "--live", action="store_true", help="also ask the configured agent one question"
    )
    doctor.set_defaults(func=_doctor)

    setup = subparsers.add_parser(
        "setup",
        help="choose the agent: Claude or Codex, on a subscription or an API key",
        description=(
            "Pick the agent backend, check the CLI and its login (or the API key), "
            "write config.toml keeping your comments, and ask one test question. "
            "Sombra never sees a subscription token: logins run in the CLIs' own flows."
        ),
    )
    setup.add_argument(
        "--non-interactive",
        action="store_true",
        help="no prompts, installs or logins; needs --backend",
    )
    setup.add_argument(
        "--backend", choices=("claude-code", "claude-api", "codex"), help="agent backend"
    )
    setup.add_argument(
        "--auth",
        choices=("subscription", "api-key"),
        help="codex only: ChatGPT plan via `codex login` (default) or an OpenAI API key",
    )
    setup.add_argument(
        "--summary-backend",
        choices=("follow", "claude-code", "claude-api", "codex", "none"),
        help="who writes summaries and minutes (default: follow the agent)",
    )
    setup.add_argument("--show", action="store_true", help="print the current provider and exit")
    setup.add_argument("--no-smoke", action="store_true", help="skip the test question")
    setup.add_argument(
        "--config", type=Path, help="user config file (default: ~/.config/sombra/config.toml)"
    )
    setup.set_defaults(func=_setup)


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


def _config_init(args: argparse.Namespace) -> int:
    from sombra.config.loader import default_config_path

    path: Path = args.config.expanduser() if args.config else default_config_path()
    if path.exists():
        sys.stdout.write(f"kept {path} (already exists)\n")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:  # never overwrite, even in a race
            f.write(DEFAULT_CONFIG_TOML)
    except FileExistsError:
        sys.stdout.write(f"kept {path} (already exists)\n")
        return 0
    sys.stdout.write(f"created {path}\n")
    return 0


def _config_path(args: argparse.Namespace) -> int:
    from sombra.config.loader import default_config_path

    sys.stdout.write(f"{default_config_path()}\n")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from sombra.config.doctor import exit_code, live_probes, render_json, render_text, run_checks

    checks = run_checks(live_probes(args.config, args.models_dir, live=args.live))
    if args.json:
        sys.stdout.write(json.dumps(render_json(checks), ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(checks))
    return exit_code(checks)


def _setup(args: argparse.Namespace) -> int:
    from sombra.config.setup import SetupError, SetupRequest, live_io, run_setup

    try:
        request = SetupRequest.from_args(args)
    except SetupError as e:
        sys.stderr.write(f"sombra setup: {e}\n")
        return 2
    return run_setup(request, live_io())
