"""``sombra start`` (live meeting, macOS) and ``sombra replay`` (recorded files).

See docs/usage.md.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

LEVELS = ("L0", "L1", "L2")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    start = subparsers.add_parser(
        "start", help="start a live meeting (macOS): record, transcribe, answer when called"
    )
    start.add_argument("name", help="meeting name, e.g. 'Daily time X'")
    start.add_argument("--profile", help="profile from ~/.config/sombra/profiles/<name>.toml")
    start.add_argument("--level", choices=LEVELS, help="autonomy level (default: config)")
    start.add_argument("--window", metavar="TITLE", help="capture only the window with this title")
    start.add_argument("--config", type=Path, help="user config file")
    start.add_argument(
        "--no-window",
        action="store_true",
        help="open the overlay in a browser tab instead of the always-on-top window",
    )
    start.set_defaults(func=run_start)

    replay = subparsers.add_parser(
        "replay", help="run the whole pipeline on recorded audio (and screenshots)"
    )
    replay.add_argument("me", help="WAV of your mic (EU); '-' for none")
    replay.add_argument("others", help="WAV of the call audio (OUTROS); '-' for none")
    replay.add_argument("--frames", type=Path, help="folder of <unix_ms>.png screenshots")
    replay.add_argument(
        "--speed", type=float, help="pace at N x real time (default: as fast as possible)"
    )
    replay.add_argument("--fake-brain", action="store_true", help="scripted answers, no API")
    replay.add_argument(
        "--auto-approve", action="store_true", help="approve every suggestion, no overlay"
    )
    replay.add_argument("--name", default="replay", help="meeting name (default: replay)")
    replay.add_argument("--level", choices=LEVELS, help="autonomy level (default: config)")
    replay.add_argument("--user", help="your name (default: [user] name in the config)")
    replay.add_argument("--stt-model", help="whisper model, e.g. tiny (default: config)")
    replay.add_argument("--root", type=Path, help="meetings folder (default: config)")
    replay.add_argument("--config", type=Path, help="user config file")
    replay.set_defaults(func=run_replay_command)


# --- helpers ---------------------------------------------------------------------------


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def report_text(meeting_dir: Path) -> str:
    """The ``sombra report`` table for one meeting."""
    from sombra.metrics import aggregate, analyze
    from sombra.metrics.render import render

    reports = [analyze(meeting_dir)]
    return render(reports, aggregate(reports), "text")


def _key_lookup() -> Callable[[], str] | None:
    """The Anthropic key as a lazy getter, or None when the keychain has none."""
    from sombra.orchestrator.wiring import ANTHROPIC
    from sombra.privacy import SecretsError, get_api_key

    try:
        get_api_key(ANTHROPIC)
    except SecretsError:
        return None
    return lambda: get_api_key(ANTHROPIC)


def _check_wav(value: str) -> Path | None:
    if value == "-":
        return None
    path = Path(value)
    with wave.open(str(path), "rb"):  # raises on a missing or non-WAV file
        pass
    return path


# --- sombra start ----------------------------------------------------------------------


def run_start(
    args: argparse.Namespace,
    *,
    platform: str = sys.platform,
    keys: Callable[[], Callable[[], str] | None] = _key_lookup,
    confirm: Callable[[str], bool] | None = None,
    runner: Callable[[Any, argparse.Namespace], None] | None = None,
    preflight: Callable[[str], object] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    from sombra.config import ConfigError, load_profile, load_user_config
    from sombra.contracts import AutonomyLevel
    from sombra.orchestrator.live import LivePlan, discard_meeting
    from sombra.orchestrator.wiring import ANTHROPIC, check_models
    from sombra.privacy import ConsentRefusedError, check_disk_encryption, require_consent
    from sombra.store import create_meeting, read_started_at

    out = out or sys.stdout
    err = err or sys.stderr
    if platform != "darwin":
        err.write("sombra start: live capture is macOS only for now; try `sombra replay`\n")
        return 2
    try:
        cfg = load_user_config(args.config)
        profile = load_profile(args.profile) if args.profile else None
    except (ConfigError, FileNotFoundError) as e:
        err.write(f"sombra start: {e}\n")
        return 2
    level = AutonomyLevel(args.level) if args.level else cfg.autonomy_level
    if level is AutonomyLevel.L3:
        err.write("sombra start: L3 (answering alone) is not available yet; use L0-L2\n")
        return 2
    if level is not AutonomyLevel.L0 and not cfg.user.name:
        err.write("sombra start: set [user] name in the config (the name you're called by)\n")
        return 2
    try:
        (preflight or check_models)(cfg.models.stt)
    except (FileNotFoundError, ValueError) as e:
        err.write(f"sombra start: {e}\n")
        return 2
    api_key = keys()
    if api_key is None:
        if level is not AutonomyLevel.L0:
            err.write(f"sombra start: no API key; run `sombra auth set {ANTHROPIC}`\n")
            return 2
        err.write("sombra start: no API key, so no minutes will be written (L0)\n")

    try:
        meeting_dir = create_meeting(
            cfg.meetings_root,
            args.name,
            profile.name if profile else None,
            context_paths=profile.context if profile else (),
            aliases=cfg.user.all_aliases,
            autonomy_level=level,
            allowed_topics=profile.allowed_topics if profile else (),
        )
    except (FileNotFoundError, ValueError) as e:
        err.write(f"sombra start: {e}\n")
        return 2
    try:
        if confirm is None:
            require_consent(meeting_dir)
        else:
            require_consent(meeting_dir, confirm=confirm)
    except ConsentRefusedError as e:
        discard_meeting(meeting_dir)
        err.write(f"sombra start: {e}\n")
        return 1
    if (warning := check_disk_encryption().warning) is not None:
        err.write(f"sombra: {warning}\n")

    plan = LivePlan(
        meeting_dir=meeting_dir,
        started_at=read_started_at(meeting_dir),
        config=cfg,
        level=level,
        allowed_topics=profile.allowed_topics if profile else (),
        window=args.window,
        api_key=api_key,
    )
    out.write(f"{meeting_dir}\n")
    out.flush()
    (runner or _run_live)(plan, args)
    out.write(report_text(meeting_dir) + "\n")
    return 0


def _run_live(plan: Any, args: argparse.Namespace) -> None:  # pragma: no cover - macOS live
    from sombra.contracts import AutonomyLevel
    from sombra.orchestrator.live import run_in_terminal, run_with_window
    from sombra.privacy import DEFAULT_SHORTCUT
    from sombra.ui.window import window_available

    _setup_logging()
    if plan.level is not AutonomyLevel.L0 and not args.no_window and window_available():
        run_with_window(plan, DEFAULT_SHORTCUT)
    else:
        run_in_terminal(plan)


# --- sombra replay ---------------------------------------------------------------------


def run_replay_command(
    args: argparse.Namespace,
    *,
    keys: Callable[[], Callable[[], str] | None] = _key_lookup,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    from sombra.config import ConfigError, load_user_config
    from sombra.contracts import AutonomyLevel

    out = out or sys.stdout
    err = err or sys.stderr
    try:
        cfg = load_user_config(args.config)
        me, others = _check_wav(args.me), _check_wav(args.others)
    except (ConfigError, OSError, wave.Error, EOFError) as e:
        err.write(f"sombra replay: {e}\n")
        return 2
    if me is None and others is None:
        err.write("sombra replay: need at least one WAV file\n")
        return 2
    if args.frames is not None and not args.frames.is_dir():
        err.write(f"sombra replay: not a folder: {args.frames}\n")
        return 2
    if args.speed is not None and args.speed <= 0:
        err.write("sombra replay: --speed must be > 0\n")
        return 2
    level = AutonomyLevel(args.level) if args.level else cfg.autonomy_level
    user = args.user or cfg.user.name
    if level is not AutonomyLevel.L0 and not user:
        err.write("sombra replay: pass --user or set [user] name in the config\n")
        return 2
    api_key = None
    needs_api = level is not AutonomyLevel.L0 and not args.fake_brain
    if needs_api:
        api_key = keys()
        if api_key is None:
            err.write("sombra replay: no API key; `sombra auth set anthropic` or --fake-brain\n")
            return 2

    from sombra.orchestrator.replay import ReplayOptions

    opts = ReplayOptions(
        me=me,
        others=others,
        frames=args.frames,
        speed=args.speed,
        meetings_root=args.root or cfg.meetings_root,
        user_name=user,
        aliases=cfg.user.aliases if not args.user else (),
        name=args.name,
        level=level,
        stt_model=args.stt_model or cfg.models.stt,
        agent_model=cfg.models.agent,
    )
    _setup_logging()
    try:
        meeting_dir = asyncio.run(
            _replay(
                opts,
                api_key,
                fake_brain=args.fake_brain,
                auto_approve=args.auto_approve,
                summary_model=cfg.models.summary,
            )
        )
    except (FileNotFoundError, ValueError) as e:  # e.g. whisper model not downloaded
        err.write(f"sombra replay: {e}\n")
        return 1
    out.write(f"{meeting_dir}\n{report_text(meeting_dir)}\n")
    return 0


async def _replay(
    opts: Any,
    api_key: Callable[[], str] | None,
    *,
    fake_brain: bool,
    auto_approve: bool,
    summary_model: str,
) -> Path:
    from sombra.contracts import Brain
    from sombra.orchestrator.replay import AutoApproveUI, ScriptedBrain, run_replay
    from sombra.orchestrator.wiring import resolve_claude_model
    from sombra.summary import AnthropicTextModel

    brain: Brain | None = ScriptedBrain() if fake_brain else None
    summary = None
    if api_key is not None:
        summary = AnthropicTextModel(api_key, model=resolve_claude_model(summary_model))
    if not auto_approve:
        return await _replay_with_overlay(opts, brain, summary, api_key)
    return await run_replay(
        opts, brain=brain, ui=AutoApproveUI(), summary_model=summary, api_key=api_key
    )


async def _replay_with_overlay(
    opts: Any, brain: Any, summary: Any, api_key: Callable[[], str] | None
) -> Path:  # pragma: no cover - interactive overlay in a browser tab
    from sombra.orchestrator.replay import run_replay
    from sombra.ui import OverlayUI
    from sombra.ui.window import open_in_browser

    overlay = OverlayUI()
    await overlay.start()
    try:
        sys.stderr.write(f"sombra: overlay em {overlay.url}\n")
        open_in_browser(overlay.url)
        return await run_replay(
            opts, brain=brain, ui=overlay, summary_model=summary, api_key=api_key
        )
    finally:
        await overlay.close()
