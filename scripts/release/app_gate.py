"""The macOS release gate: is this ``Sombra.app`` fit to publish for this tag? (ADR 0050)

Sombra.app is signed with the project's own **self-signed** certificate (no paid Apple
Developer ID, no notarization). What makes TCC grants survive upgrades is that every
release is signed by the *same* certificate, so its SHA-256 is pinned in the repo
(``packaging/macos/signing-cert.sha256``) and checked here. Two steps, so the evidence
is gathered on a Mac and the decision is unit-tested::

    # on macOS, against the app unpacked from the zip that would be published
    python scripts/release/app_gate.py record --app Sombra.app \\
        --zip Sombra-0.2.0-macos-arm64.zip --output app-status.json

    # anywhere: decide, for a tag, from that record
    python scripts/release/app_gate.py decide --status app-status.json --tag v0.2.0 \\
        --zip Sombra-0.2.0-macos-arm64.zip [--pinned-sha256 HEX] [--github-output FILE]

``record`` runs ``codesign -dv``, ``codesign -d -r-`` (the designated requirement),
``codesign -d --extract-certificates`` (the leaf certificate's SHA-256) and
``codesign --verify --deep --strict``, and writes what they said. ``decide`` then:

- **refuses** (exit 1) a final tag unless the app is signed by the pinned certificate,
  with the hardened runtime, a designated requirement that pins that certificate (not
  the cdhash), and a clean strict verification;
- lets a pre-release publish an **ad-hoc** app, labelled as such (``app_signature=adhoc``:
  its grants reset on every upgrade); a pre-release signed by any other certificate is
  refused, since that means the signing secrets are wrong;
- always refuses a zip whose name, SHA-256, version or bundle id don't match.

It prints ``attach=true|false`` and ``app_signature=pinned|adhoc`` (also to
``--github-output``). ``ready --status FILE`` exits 1, saying why, when the recorded app
could not ship in a final release. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging" / "macos"))

import sombra_app  # noqa: E402  # packaging/macos: bundle id and asset name
from tags import TagError, parse_tag  # noqa: E402  # scripts/release, next to this file

PINNED_FILE = ROOT / "packaging" / "macos" / "signing-cert.sha256"

CERTIFICATE = "certificate"  # signed with a certificate (ours or anyone's)
ADHOC = "adhoc"
UNSIGNED = "unsigned"

_RUNTIME_FLAG = re.compile(r"\bflags=0x[0-9a-f]+\([^)]*\bruntime\b[^)]*\)")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Signature:
    kind: str  # CERTIFICATE, ADHOC or UNSIGNED
    authority: str = ""  # the leaf certificate's common name
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
    authority: str
    identifier: str
    cert_sha256: str
    designated_requirement: str
    hardened_runtime: bool
    codesign_strict: bool


@dataclass
class Decision:
    attach: bool
    refuse: bool
    app_signature: str = ""  # "pinned" or "adhoc" when attached
    reasons: list[str] = field(default_factory=list)


# --- parsing tool output -----------------------------------------------------------------


def parse_codesign_display(text: str) -> Signature:
    """``codesign -dv --verbose=4`` output (it writes to stderr) -> what signed it."""
    if "code object is not signed at all" in text:
        return Signature(UNSIGNED)
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields.setdefault(key.strip(), []).append(value.strip())
    identifier = fields.get("Identifier", [""])[0]
    # "CodeDirectory v=20500 size=... flags=0x10002(adhoc,runtime) hashes=..."
    runtime = bool(_RUNTIME_FLAG.search(text))
    authorities = fields.get("Authority", [])
    if "adhoc" in fields.get("Signature", []) or not authorities:
        return Signature(ADHOC, "", identifier, runtime)
    return Signature(CERTIFICATE, authorities[0], identifier, runtime)


def parse_requirement(text: str) -> str:
    """``codesign -d -r-`` output -> the designated requirement expression."""
    for line in text.splitlines():
        key, sep, value = line.partition("=>")
        if sep and key.strip() == "designated":
            return value.strip()
    return ""


def pins_certificate(requirement: str, identifier: str) -> bool:
    """True when the designated requirement names the identifier and a certificate hash.

    A self-signed identity gets ``identifier "X" and certificate leaf = H"<sha1>"``; an
    ad-hoc signature gets ``cdhash H"..."``, which changes with every build.
    """
    return (
        f'identifier "{identifier}"' in requirement
        and re.search(r'certificate (?:leaf|root) = H"[0-9a-fA-F]{40}"', requirement) is not None
        and "cdhash" not in requirement
    )


def normalize_fingerprint(text: str) -> str:
    return text.strip().replace(":", "").lower()


def read_pinned(path: Path | None = None) -> str:
    """The pinned certificate SHA-256 (first non-comment line), or "" if none is set."""
    path = PINNED_FILE if path is None else path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        value = normalize_fingerprint(line.split("#", 1)[0])
        if value:
            if not _HEX64.match(value):
                raise ValueError(f"{path}: not a SHA-256 fingerprint: {line.strip()!r}")
            return value
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


def leaf_certificate_sha256(app: Path, tool: Tool = _tool) -> str:
    """SHA-256 of the signing (leaf) certificate, or "" for ad-hoc/unsigned code."""
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "cert"
        tool(["codesign", "-d", f"--extract-certificates={prefix}", str(app)])
        leaf = Path(f"{prefix}0")
        return sha256_file(leaf) if leaf.is_file() else ""


def record(
    app: Path,
    zip_path: Path,
    tool: Tool = _tool,
    cert_sha256: Callable[[Path], str] | None = None,
) -> AppStatus:
    with (app / "Contents" / "Info.plist").open("rb") as f:
        info = plistlib.load(f)
    _, display = tool(["codesign", "-dv", "--verbose=4", str(app)])
    sig = parse_codesign_display(display)
    _, req = tool(["codesign", "-d", "-r-", str(app)])
    strict, _ = tool(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    leaf = cert_sha256(app) if cert_sha256 else leaf_certificate_sha256(app, tool)
    return AppStatus(
        zip=zip_path.name,
        sha256=sha256_file(zip_path),
        bundle_id=str(info.get("CFBundleIdentifier", "")),
        version=str(info.get("CFBundleShortVersionString", "")),
        signature=sig.kind,
        authority=sig.authority,
        identifier=sig.identifier,
        cert_sha256=leaf,
        designated_requirement=parse_requirement(req),
        hardened_runtime=sig.hardened_runtime,
        codesign_strict=strict == 0,
    )


def load_status(path: Path) -> AppStatus:
    return AppStatus(**json.loads(path.read_text(encoding="utf-8")))


# --- decide --------------------------------------------------------------------------------


def not_release_ready(status: AppStatus, pinned: str) -> list[str]:
    """Why this app may not ship in a final release; empty when it may."""
    reasons = []
    if status.signature != CERTIFICATE:
        reasons.append(
            f"signature is {status.signature}, not the project's certificate "
            "(an ad-hoc app makes users grant the permissions again on every upgrade)"
        )
    elif not pinned:
        reasons.append(
            "no certificate is pinned in packaging/macos/signing-cert.sha256 "
            "(run scripts/release/make-signing-identity.sh once, see docs/release.md)"
        )
    elif status.cert_sha256 != pinned:
        reasons.append(
            f"signed by certificate {status.cert_sha256 or 'unknown'} "
            f"({status.authority or 'no name'}), but {pinned} is pinned"
        )
    if status.signature == CERTIFICATE and not pins_certificate(
        status.designated_requirement, sombra_app.BUNDLE_ID
    ):
        reasons.append(
            "designated requirement does not pin the identifier and certificate: "
            f"{status.designated_requirement or 'none'}"
        )
    if not status.hardened_runtime:
        reasons.append("hardened runtime is off")
    if not status.codesign_strict:
        reasons.append("codesign --verify --deep --strict failed")
    return reasons


def decide(status: AppStatus, tag_name: str, zip_path: Path, pinned: str) -> Decision:
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
    reasons = not_release_ready(status, pinned)
    if not reasons:
        return Decision(attach=True, refuse=False, app_signature="pinned")
    usable_adhoc = status.signature == ADHOC and status.codesign_strict
    if tag.prerelease and usable_adhoc:
        return Decision(attach=True, refuse=False, app_signature="adhoc", reasons=reasons)
    return Decision(attach=False, refuse=True, reasons=reasons)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    rec = sub.add_parser("record", help="inspect the app on macOS and write its status")
    rec.add_argument("--app", type=Path, required=True)
    rec.add_argument("--zip", type=Path, required=True)
    rec.add_argument("--output", type=Path, required=True)
    pin_help = f"the certificate SHA-256 to require (default: {PINNED_FILE.name})"
    dec = sub.add_parser("decide", help="may this tag publish this app?")
    dec.add_argument("--status", type=Path, required=True)
    dec.add_argument("--tag", required=True)
    dec.add_argument("--zip", type=Path, required=True)
    dec.add_argument("--pinned-sha256", help=pin_help)
    dec.add_argument("--github-output", type=Path)
    rdy = sub.add_parser("ready", help="could this app ship in a final release?")
    rdy.add_argument("--status", type=Path, required=True)
    rdy.add_argument("--pinned-sha256", help=pin_help)
    args = parser.parse_args(argv)

    if args.action == "record":  # pragma: no cover - runs the macOS tools
        status = record(args.app, args.zip)
        text = json.dumps(asdict(status), indent=2) + "\n"
        args.output.write_text(text, encoding="utf-8")
        sys.stdout.write(text)
        return 0

    pinned = normalize_fingerprint(args.pinned_sha256) if args.pinned_sha256 else read_pinned()
    if args.action == "ready":
        reasons = not_release_ready(load_status(args.status), pinned)
        for reason in reasons:
            sys.stdout.write(f"not release-ready: {reason}\n")
        if not reasons:
            sys.stdout.write(f"release-ready: signed by the pinned certificate {pinned}\n")
        return 1 if reasons else 0

    decision = decide(load_status(args.status), args.tag, args.zip, pinned)
    for reason in decision.reasons:
        level = "error" if decision.refuse else "warning"
        sys.stderr.write(f"::{level}::app gate ({args.tag}): {reason}\n")
    if decision.refuse:
        sys.stderr.write(
            f"app gate: refusing to publish {args.tag}: a final release needs Sombra.app "
            "signed by the pinned certificate (docs/release.md)\n"
        )
        return 1
    lines = f"attach={str(decision.attach).lower()}\napp_signature={decision.app_signature}\n"
    sys.stdout.write(lines)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
