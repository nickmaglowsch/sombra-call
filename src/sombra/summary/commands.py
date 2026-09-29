"""``sombra minutes <meeting>``: (re)generate the minutes of a finished meeting (M3).

See docs/summary.md for the key lookup (OS keychain, then ``ANTHROPIC_API_KEY``).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_ROOT = Path("~/Sombra/meetings")
KEYRING_SERVICE = "sombra"
KEYRING_USER = "anthropic-api-key"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "minutes", help="(re)generate the minutes (## Ata) in a meeting's summary.md"
    )
    p.add_argument("meeting", help="meeting folder, or its name under ~/Sombra/meetings")
    p.add_argument("--model", default=None, help="Anthropic model id (default: cheap model)")
    p.set_defaults(func=_run)


def resolve_meeting(meeting: str, root: Path = DEFAULT_ROOT) -> Path:
    path = Path(meeting).expanduser()
    if path.is_dir():
        return path
    return root.expanduser() / meeting


def api_key() -> str:
    """Anthropic key from the OS keychain (Keychain / Secret Service), else the env."""
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:  # keyring missing or no backend: fall back to env
        key = None
    key = key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            f"no API key: store it with `keyring set {KEYRING_SERVICE} {KEYRING_USER}` "
            "or export ANTHROPIC_API_KEY"
        )
    return key


def _run(args: argparse.Namespace) -> int:
    from sombra.summary.minutes import TRANSCRIPT_FILE, write_minutes
    from sombra.summary.model import DEFAULT_MODEL, AnthropicTextModel

    meeting_dir = resolve_meeting(args.meeting)
    if not (meeting_dir / TRANSCRIPT_FILE).is_file():
        sys.stderr.write(f"sombra minutes: no {TRANSCRIPT_FILE} in {meeting_dir}\n")
        return 2
    model = AnthropicTextModel(api_key, model=args.model or DEFAULT_MODEL)
    try:
        result = write_minutes(meeting_dir, model)
    except Exception as e:  # any API or parse failure is exit code 1
        sys.stderr.write(f"sombra minutes: {e}\n")
        return 1
    u = result.usage
    sys.stdout.write(
        f"Ata escrita em {meeting_dir / 'summary.md'}: {len(result.minutes.actions)} itens de "
        f"ação, {len(result.dropped)} descartados (horário inexistente); {result.calls} "
        f"chamadas, {u.input_tokens} tokens de entrada, {u.output_tokens} de saída.\n"
    )
    return 0
