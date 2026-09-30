"""The macOS release gate: is this ``Sombra.app`` fit to publish for this tag? (ADR 0050)

Two steps, so the evidence is gathered on a Mac and the decision is unit-tested::

    # on macOS, against the app unpacked from the zip that would be published
    python scripts/release/app_gate.py record --app Sombra.app \\
        --zip Sombra-0.2.0-macos-arm64.zip --output app-status.json

    # anywhere: decide, for a tag, from that record
    python scripts/release/app_gate.py decide --status app-status.json --tag v0.2.0 \\
        --zip Sombra-0.2.0-macos-arm64.zip [--team-id ABCDE12345] [--github-output FILE]

``record`` runs ``codesign -dv``, ``codesign --verify --deep --strict``,
``spctl --assess --type execute`` and ``stapler validate`` and writes what they said.
``decide`` then:

- **refuses** (exit 1) a final tag unless the app is Developer ID signed with the
  hardened runtime, passes the strict verification, is notarized (Gatekeeper accepts it
  as "Notarized Developer ID") and has its ticket stapled;
- lets a pre-release publish **without** the app when it is not (``attach=false``): an
  unsigned app is never a release asset;
- always refuses a zip whose name, SHA-256, version or bundle id don't match.

It prints ``attach=true|false`` (also to ``--github-output``). ``ready --status FILE``
exits 1 and says why when the recorded app could not ship in a final release: PR runs
use it to show the gate refuses their ad-hoc build. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packaging" / "macos"))

import sombra_app  # packaging/macos: bundle id and asset name
from tags import TagError, parse_tag  # scripts/release, next to this file

DEVELOPER_ID = "developer-id"
ADHOC = "adhoc"
OTHER = "other"
UNSIGNED = "unsigned"


@dataclass(frozen=True)
class Signature:
    kind: str  # DEVELOPER_ID, ADHOC, OTHER or UNSIGNED
    team_id: str = ""
    identifier: str = ""
    hardened_runtime: bool = False


@dataclass
class AppStatus:
    """What ``record`` saw. Written as JSON; ``decide`` reads it back."""

    zip: str
    sha256: str
    bundle_id: str
    version: str
    signature: str
    team_id: str
    identifier: str
    hardened_runtime: bool
    codesign_strict: bool
    spctl: bool
    spctl_source: str
    stapled: bool

    @property
    def notarized(self) -> bool:
        return self.spctl and "notarized" in self.spctl_source.lower()


@dataclass
class Decision:
    attach: bool
    refuse: bool
    reasons: list[str] = field(default_factory=list)


# --- parsing tool output -----------------------------------------------------------------

_RUNTIME_FLAG = re.compile(r"\bflags=0x[0-9a-f]+\([^)]*\bruntime\b[^)]*\)")


def parse_codesign_display(text: str) -> Signature:
    """``codesign -dv --verbose=4`` output (it writes to stderr) -> what signed it."""
    if "code object is not signed at all" in text:
        return Signature(UNSIGNED)
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields.setdefault(key.strip(), []).append(value.strip())
    team = fields.get("TeamIdentifier", [""])[0]
    if team == "not set":
        team = ""
    identifier = fields.get("Identifier", [""])[0]
    # "CodeDirectory v=20500 size=... flags=0x10002(adhoc,runtime) hashes=..."
    runtime = bool(_RUNTIME_FLAG.search(text))
    if "adhoc" in fields.get("Signature", []):
        kind = ADHOC
    elif any(a.startswith("Developer ID Application:") for a in fields.get("Authority", [])):
        kind = DEVELOPER_ID
    else:
        kind = OTHER
    return Signature(kind, team, identifier, runtime)


def parse_spctl_source(text: str) -> str:
    """``spctl --assess -vv`` output -> its ``source=`` value ("Notarized Developer ID")."""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "source":
            return value.strip()
    return ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


# --- record (macOS) ------------------------------------------------------------------------

Tool = Callable[[Sequence[str]], tuple[int, str]]


def _tool(cmd: Sequence[str]) -> tuple[int, str]:  # pragma: no cover - macOS tools
    proc = subprocess.run(list(cmd), capture_output=True, text=True, check=False)  # noqa: S603  # fixed argv
    return proc.returncode, proc.stdout + proc.stderr


def record(app: Path, zip_path: Path, tool: Tool = _tool) -> AppStatus:
    with (app / "Contents" / "Info.plist").open("rb") as f:
        info = plistlib.load(f)
    _, display = tool(["codesign", "-dv", "--verbose=4", str(app)])
    sig = parse_codesign_display(display)
    strict, _ = tool(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    spctl, assess = tool(["spctl", "--assess", "--type", "execute", "-vv", str(app)])
    stapled, _ = tool(["xcrun", "stapler", "validate", str(app)])
    return AppStatus(
        zip=zip_path.name,
        sha256=sha256_file(zip_path),
        bundle_id=str(info.get("CFBundleIdentifier", "")),
        version=str(info.get("CFBundleShortVersionString", "")),
        signature=sig.kind,
        team_id=sig.team_id,
        identifier=sig.identifier,
        hardened_runtime=sig.hardened_runtime,
        codesign_strict=strict == 0,
        spctl=spctl == 0,
        spctl_source=parse_spctl_source(assess),
        stapled=stapled == 0,
    )


def load_status(path: Path) -> AppStatus:
    return AppStatus(**json.loads(path.read_text(encoding="utf-8")))


# --- decide --------------------------------------------------------------------------------


def not_release_ready(status: AppStatus, team_id: str = "") -> list[str]:
    """Why this app may not ship in a final release; empty when it may."""
    reasons = []
    if status.signature != DEVELOPER_ID:
        reasons.append(f"signature is {status.signature}, not Developer ID Application")
    if team_id and status.team_id != team_id:
        reasons.append(f"team id is {status.team_id or 'not set'}, expected {team_id}")
    if not status.hardened_runtime:
        reasons.append("hardened runtime is off")
    if not status.codesign_strict:
        reasons.append("codesign --verify --deep --strict failed")
    if not status.notarized:
        reasons.append(
            "not notarized (spctl --assess --type execute: "
            f"{'accepted' if status.spctl else 'rejected'}, "
            f"source={status.spctl_source or 'none'})"
        )
    if not status.stapled:
        reasons.append("no stapled notarization ticket")
    return reasons


def decide(status: AppStatus, tag_name: str, zip_path: Path, team_id: str = "") -> Decision:
    try:
        tag = parse_tag(tag_name)
    except TagError as e:
        return Decision(attach=False, refuse=True, reasons=[str(e)])
    integrity = []
    expected_zip = sombra_app.zip_name(tag.version)
    if zip_path.name != expected_zip:
        integrity.append(f"asset is {zip_path.name}, expected {expected_zip}")
    if not zip_path.is_file():
        integrity.append(f"{zip_path} not found")
    elif sha256_file(zip_path) != status.sha256:
        integrity.append(f"{zip_path.name} is not the zip that was verified (sha256 differs)")
    if status.version != tag.version:
        integrity.append(f"CFBundleShortVersionString is {status.version}, tag means {tag.version}")
    if status.bundle_id != sombra_app.BUNDLE_ID:
        integrity.append(f"bundle id is {status.bundle_id}, expected {sombra_app.BUNDLE_ID}")
    if integrity:
        return Decision(attach=False, refuse=True, reasons=integrity)
    reasons = not_release_ready(status, team_id)
    if not reasons:
        return Decision(attach=True, refuse=False)
    if tag.prerelease:
        return Decision(attach=False, refuse=False, reasons=reasons)
    return Decision(attach=False, refuse=True, reasons=reasons)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    rec = sub.add_parser("record", help="inspect the app on macOS and write its status")
    rec.add_argument("--app", type=Path, required=True)
    rec.add_argument("--zip", type=Path, required=True)
    rec.add_argument("--output", type=Path, required=True)
    dec = sub.add_parser("decide", help="may this tag publish this app?")
    dec.add_argument("--status", type=Path, required=True)
    dec.add_argument("--tag", required=True)
    dec.add_argument("--zip", type=Path, required=True)
    dec.add_argument("--team-id", default="", help="the Developer ID team it must carry")
    dec.add_argument("--github-output", type=Path)
    rdy = sub.add_parser("ready", help="could this app ship in a final release?")
    rdy.add_argument("--status", type=Path, required=True)
    rdy.add_argument("--team-id", default="")
    args = parser.parse_args(argv)

    if args.action == "record":  # pragma: no cover - runs the macOS tools
        status = record(args.app, args.zip)
        text = json.dumps(asdict(status), indent=2) + "\n"
        args.output.write_text(text, encoding="utf-8")
        sys.stdout.write(text)
        return 0

    if args.action == "ready":
        reasons = not_release_ready(load_status(args.status), args.team_id)
        for reason in reasons:
            sys.stdout.write(f"not release-ready: {reason}\n")
        if not reasons:
            sys.stdout.write("release-ready: Developer ID, hardened runtime, notarized, stapled\n")
        return 1 if reasons else 0

    decision = decide(load_status(args.status), args.tag, args.zip, args.team_id)
    for reason in decision.reasons:
        level = "error" if decision.refuse else "warning"
        sys.stderr.write(f"::{level}::app gate ({args.tag}): {reason}\n")
    if decision.refuse:
        sys.stderr.write(
            f"app gate: refusing to publish {args.tag}: a final release needs Sombra.app "
            "Developer ID signed, notarized and stapled (docs/release.md)\n"
        )
        return 1
    line = f"attach={str(decision.attach).lower()}\n"
    sys.stdout.write(line)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
