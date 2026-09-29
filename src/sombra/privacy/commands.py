"""``sombra auth``, ``sombra retention``, ``sombra delete``, ``sombra pause`` / ``resume``."""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

DEFAULT_MEETINGS_ROOT = Path("~/Sombra/meetings")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    auth = subparsers.add_parser("auth", help="store or remove an API key in the OS keychain")
    auth_sub = auth.add_subparsers(dest="auth_command", metavar="<set|clear|status>", required=True)
    p = auth_sub.add_parser("set", help="prompt for a key and store it in the keychain")
    p.add_argument("provider", help="e.g. anthropic, openai")
    p.add_argument(
        "--stdin", action="store_true", help="read the key from stdin instead of prompting"
    )
    p.set_defaults(func=_auth_set)
    p = auth_sub.add_parser("clear", help="remove a stored key")
    p.add_argument("provider")
    p.set_defaults(func=_auth_clear)
    p = auth_sub.add_parser("status", help="say whether a key is stored (never prints it)")
    p.add_argument("provider")
    p.set_defaults(func=_auth_status)

    ret = subparsers.add_parser("retention", help="delete expired frames and transcripts")
    ret_sub = ret.add_subparsers(dest="retention_command", metavar="<run>", required=True)
    p = ret_sub.add_parser("run", help="sweep the meetings folder now")
    _add_root(p)
    p.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    p.add_argument("--frames-days", type=int, default=7, help="keep frames this long (7)")
    p.add_argument(
        "--text-days", type=int, default=30, help="keep transcript/summary/log this long (30)"
    )
    p.set_defaults(func=_retention_run)

    p = subparsers.add_parser("delete", help="delete a whole meeting folder")
    p.add_argument("meeting", help="meeting folder name (or path) inside the meetings root")
    _add_root(p)
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=_delete)

    for name, text in (("pause", "pause capture and STT"), ("resume", "resume capture and STT")):
        p = subparsers.add_parser(name, help=f"{text} in the running session")
        p.add_argument("--socket", type=Path, default=None, help="control socket path")
        p.set_defaults(func=_control, control_command=name)


def _add_root(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_MEETINGS_ROOT,
        help="meetings root (default ~/Sombra/meetings)",
    )


def _err(msg: str) -> int:
    print(f"sombra: {msg}", file=sys.stderr)  # noqa: T201 - CLI output
    return 1


def _out(msg: str) -> None:
    print(msg)  # noqa: T201 - CLI output


def _auth_set(args: argparse.Namespace, stdin: TextIO | None = None) -> int:
    from sombra.privacy.secrets import SecretsError, set_api_key

    if args.stdin:
        key = (stdin or sys.stdin).readline()
    else:
        key = getpass.getpass(f"{args.provider} API key (input hidden): ")
    try:
        set_api_key(args.provider, key)
    except (SecretsError, ValueError) as exc:
        return _err(str(exc))
    _out(f"{args.provider} API key stored in the OS keychain")
    return 0


def _auth_clear(args: argparse.Namespace) -> int:
    from sombra.privacy.secrets import SecretsError, clear_api_key

    try:
        removed = clear_api_key(args.provider)
    except (SecretsError, ValueError) as exc:
        return _err(str(exc))
    _out(f"{args.provider} API key removed" if removed else f"no {args.provider} API key stored")
    return 0


def _auth_status(args: argparse.Namespace) -> int:
    from sombra.privacy.secrets import SecretsError, has_api_key

    try:
        stored = has_api_key(args.provider)
    except (SecretsError, ValueError) as exc:
        return _err(str(exc))
    _out(f"{args.provider}: {'stored' if stored else 'not set'}")
    return 0 if stored else 1


def _retention_run(args: argparse.Namespace) -> int:
    from sombra.privacy.retention import RetentionPolicy, UnsafePathError, sweep

    try:
        policy = RetentionPolicy(frames_days=args.frames_days, text_days=args.text_days)
        report = sweep(
            args.root.expanduser(), datetime.now(UTC).astimezone(), policy, dry_run=args.dry_run
        )
    except (UnsafePathError, ValueError, FileNotFoundError) as exc:
        return _err(str(exc))
    verb = "would delete" if args.dry_run else "deleted"
    for path in report.deleted:
        _out(f"{verb} {path}")
    for path in report.skipped:
        _out(f"skipped (symlink or outside root) {path}")
    _out(f"{len(report.deleted)} {verb}, {len(report.skipped)} skipped")
    return 0


def _delete(args: argparse.Namespace) -> int:
    from sombra.privacy.retention import UnsafePathError, delete_meeting, resolve_meeting

    root = args.root.expanduser()
    try:
        path = resolve_meeting(root, args.meeting)
    except (UnsafePathError, FileNotFoundError) as exc:
        return _err(str(exc))
    if not args.yes:
        try:
            answer = input(f"Delete {path} and everything in it? Type the folder name to confirm: ")
        except EOFError:
            answer = ""
        if answer.strip() != path.name:
            return _err("not confirmed; nothing deleted")
    try:
        delete_meeting(root, path, confirmed=True)
    except (UnsafePathError, OSError) as exc:
        return _err(str(exc))
    _out(f"deleted {path}")
    return 0


def _control(args: argparse.Namespace) -> int:
    from sombra.privacy.control import ControlSocketError, send_command

    try:
        reply = send_command(args.control_command, args.socket)
    except (ControlSocketError, OSError) as exc:
        return _err(str(exc))
    _out(reply)
    return 0 if reply.startswith("ok") else 1
