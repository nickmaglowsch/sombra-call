"""``sombra ask``: ask a question about a recorded meeting (see :mod:`.ask`)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from sombra.config import UserConfig
    from sombra.contracts import Brain

    BrainFactory = Callable[[Path, UserConfig, argparse.Namespace], Brain]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "ask",
        help="ask a question about a recorded meeting",
        description=(
            "Answer a question from the meeting's transcript, summary and context files, "
            "with [HH:MM:SS] references. The agent is read-only on the meeting folder."
        ),
    )
    p.add_argument(
        "meeting",
        help="'latest', a meeting folder path, a folder name, or a meeting name ('Daily time X')",
    )
    p.add_argument("question", help='e.g. "O que combinamos sobre o prazo?"')
    p.add_argument(
        "--frames",
        action="store_true",
        help="let the agent look at screenshots (frames/) by their TELA id",
    )
    p.add_argument("--model", help="agent model id or alias (default: [models] agent in config)")
    p.add_argument(
        "--config", type=Path, help="user config file (default: ~/.config/sombra/config.toml)"
    )
    p.set_defaults(func=_ask)


def _ask(args: argparse.Namespace) -> int:
    return run(args)


def _claude(meeting_dir: Path, cfg: UserConfig, args: argparse.Namespace) -> Brain:
    from sombra.orchestrator.ask import claude_brain

    return claude_brain(meeting_dir, cfg, frames=args.frames, model=args.model)


def run(
    args: argparse.Namespace,
    *,
    make_brain: BrainFactory | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Resolve the meeting, ask, print the answer on stdout and the details on stderr."""
    from sombra.config import ConfigError, load_user_config
    from sombra.orchestrator.ask import (
        ApiKeyMissingError,
        MeetingNotFoundError,
        ask,
        resolve_meeting,
    )
    from sombra.orchestrator.session import local_now

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    factory = make_brain if make_brain is not None else _claude
    try:
        cfg = load_user_config(args.config)
        meeting_dir = resolve_meeting(args.meeting, cfg.meetings_root)
        if not args.question.strip():
            raise ValueError("the question is empty")
        brain = factory(meeting_dir, cfg, args)
    except (ConfigError, MeetingNotFoundError, ApiKeyMissingError, ValueError) as e:
        err.write(f"sombra ask: {e}\n")
        return 2
    err.write(f"reunião: {meeting_dir}\n")
    try:
        result = asyncio.run(
            ask(brain, meeting_dir, args.question, frames=args.frames, clock=local_now)
        )
    except Exception as e:  # any backend failure: report it, never a traceback
        err.write(f"sombra ask: o agente falhou ({type(e).__name__}): {e}\n")
        return 1
    r = result.response
    out.write(r.text.rstrip("\n") + "\n")
    details = [f"modelo: {r.model}", f"{result.elapsed_s:.1f} s"]
    if r.frames_sent:
        details.append("telas: " + ", ".join(r.frames_sent))
    u = r.usage
    details.append(
        f"tokens: {u.input_tokens} in, {u.output_tokens} out, "
        f"{u.cache_read_input_tokens} cache read, {u.cache_creation_input_tokens} cache write"
    )
    err.write(" · ".join(details) + "\n")
    return 0
