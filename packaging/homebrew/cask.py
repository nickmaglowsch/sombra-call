"""Render the Homebrew cask for ``Sombra.app`` and bump it in the tap (issue #51).

The cask installs the self-signed, not notarized ``Sombra.app`` that the release
workflow publishes as ``Sombra-<version>-macos-arm64.zip`` (issue #50), checked against
the release's ``SHA256SUMS``::

    python packaging/homebrew/cask.py render --version v0.3.0 --sums SHA256SUMS
    python packaging/homebrew/cask.py render --version 0.3.0 --zip S.zip --url file:///…/S.zip
    python packaging/homebrew/cask.py bump --version v0.3.0 --sums SHA256SUMS --tap-dir tap

``render`` prints the cask (or writes it with ``--output``). ``bump`` writes
``Casks/sombra.rb`` in a checkout of the tap and prints the unified diff; it never moves
the cask to a lower version, so a patch to an older line (``v0.2.1`` after
``v0.3.0``) never downgrades ``brew upgrade`` users. It prints ``changed=true|false``
last, and appends it to ``--github-output`` when given. Committing and pushing is the
workflow's job (``.github/workflows/homebrew.yml``).

Standard library only: the workflow runs it before anything is installed.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import sys
from collections.abc import Sequence
from pathlib import Path

REPO = "nickmaglowsch/sombra-call"
TEMPLATE = Path(__file__).resolve().with_name("sombra.rb.tmpl")
CASK_PATH = Path("Casks") / "sombra.rb"
# The CLI shim inside the bundle (#50). Homebrew links it into its bin directory, and it
# launches through the bundle so macOS attributes the TCC grants to Sombra.
SHIM = "Sombra.app/Contents/Resources/bin/sombra"
# The URL keeps ``#{version}`` so the cask reads like any other and livecheck can follow it.
DEFAULT_URL = (
    f"https://github.com/{REPO}/releases/download/v#{{version}}/Sombra-#{{version}}-macos-arm64.zip"
)

_FINAL = re.compile(r"^v?(?P<v>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))$")
# Any version Homebrew accepts in a string, for a local smoke test of a pre-release zip.
_ANY = re.compile(r"^v?(?P<v>[0-9][0-9A-Za-z.\-]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# What may go inside the cask's double-quoted Ruby strings: no quote, backslash or `#`,
# so nothing can close the string or interpolate (`#{…}`) Ruby code.
_SAFE_VERSION = re.compile(r"[0-9][0-9A-Za-z.\-]*")
_SAFE_URL = re.compile(r"(?:https|file)://[A-Za-z0-9._~/%:+@-]+")
_CASK_VERSION = re.compile(r'^\s*version\s+"(?P<v>[^"]+)"', re.MULTILINE)
_PLACEHOLDER = re.compile(r"@@[A-Z0-9_]+@@")


class CaskError(Exception):
    """Bad input: a malformed version or sums file, or an asset missing from it."""


def asset_name(version: str) -> str:
    """The release asset #50 publishes: a ditto zip of ``Sombra.app``."""
    return f"Sombra-{version}-macos-arm64.zip"


def final_version(tag_or_version: str) -> str:
    """``v0.3.0`` or ``0.3.0`` → ``0.3.0``. Pre-releases never go in the tap."""
    m = _FINAL.match(tag_or_version.strip())
    if m is None:
        raise CaskError(f"{tag_or_version!r} is not a final release version (vX.Y.Z)")
    return m["v"]


def any_version(tag_or_version: str) -> str:
    m = _ANY.match(tag_or_version.strip())
    if m is None:
        raise CaskError(f"{tag_or_version!r} is not a version")
    return m["v"]


def parse_sums(text: str) -> dict[str, str]:
    """``{file name: hex digest}`` from ``sha256sum`` output (text or ``*`` binary mode)."""
    sums: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        digest, sep, name = line.partition(" ")
        name = name.lstrip(" ").removeprefix("*")
        digest = digest.lower()
        if not sep or not _SHA256.match(digest) or not name:
            raise CaskError(f"SHA256SUMS line {n} is malformed: {raw!r}")
        if name in sums and sums[name] != digest:
            raise CaskError(f"SHA256SUMS lists {name!r} twice with different digests")
        sums[name] = digest
    return sums


def sha256_for(sums_text: str, version: str) -> str:
    name = asset_name(version)
    sums = parse_sums(sums_text)
    if name not in sums:
        raise CaskError(f"SHA256SUMS has no {name} (listed: {', '.join(sorted(sums)) or 'none'})")
    return sums[name]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def render(version: str, sha256: str, *, url: str = DEFAULT_URL, template: Path = TEMPLATE) -> str:
    """The cask text. ``url`` defaults to the GitHub release asset for ``version``."""
    if not _SHA256.match(sha256):
        raise CaskError(f"{sha256!r} is not a SHA-256 hex digest")
    if not _SAFE_VERSION.fullmatch(version):
        raise CaskError(f"version {version!r} cannot go in the cask")
    if url != DEFAULT_URL and not _SAFE_URL.fullmatch(url):
        raise CaskError(f"url {url!r} cannot go in the cask (https:// or file://, no # or quotes)")
    text = template.read_text(encoding="utf-8")
    for key, value in {"VERSION": version, "SHA256": sha256, "URL": url, "SHIM": SHIM}.items():
        text = text.replace(f"@@{key}@@", value)
    left = _PLACEHOLDER.findall(text)
    if left:
        raise CaskError(f"template placeholders left unfilled: {', '.join(sorted(set(left)))}")
    return text


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in final_version(version).split("."))


def tap_version(cask_text: str) -> str | None:
    m = _CASK_VERSION.search(cask_text)
    return m["v"] if m else None


def bump(tap_dir: Path, text: str, version: str, *, dry_run: bool) -> tuple[bool, str]:
    """Write the cask into the tap checkout, unless that would not move it forward.

    Returns ``(changed, message)``: the unified diff when the cask changes, otherwise
    why nothing changed. A lower version than the tap's is skipped, not an error, so a
    patch to an older line is a clean no-op. With ``dry_run`` nothing is written.
    """
    path = tap_dir / CASK_PATH
    old = path.read_text(encoding="utf-8") if path.is_file() else ""
    current = tap_version(old)
    if current and _FINAL.match(current) and version_key(current) > version_key(version):
        return False, f"the tap has sombra {current}, newer than {version}; not downgrading\n"
    if old == text:
        return False, f"the tap already has this cask for sombra {version}; nothing to push\n"
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"a/{CASK_PATH.as_posix()}",
            tofile=f"b/{CASK_PATH.as_posix()}",
        )
    )
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return True, diff


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)

    rend = sub.add_parser("render", help="print the cask for one version")
    rend.add_argument("--version", required=True, help="vX.Y.Z or X.Y.Z")
    src = rend.add_mutually_exclusive_group(required=True)
    src.add_argument("--sums", type=Path, help="the release's SHA256SUMS")
    src.add_argument("--zip", type=Path, help="hash this local zip instead (smoke tests)")
    rend.add_argument("--url", help="download URL, e.g. file:///… (default: the release asset)")
    rend.add_argument("--output", type=Path, help="write here instead of stdout")

    bmp = sub.add_parser("bump", help="write the cask into a checkout of the tap")
    bmp.add_argument("--version", required=True, help="vX.Y.Z or X.Y.Z (final only)")
    bmp.add_argument("--sums", type=Path, required=True, help="the release's SHA256SUMS")
    bmp.add_argument("--tap-dir", type=Path, required=True, help="checkout of the tap repo")
    bmp.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    bmp.add_argument("--github-output", type=Path, help="append changed=true|false here")

    args = parser.parse_args(argv)
    try:
        if args.action == "render":
            # A custom URL (a local zip) may carry a pre-release; the release URL may not.
            version = any_version(args.version) if args.url else final_version(args.version)
            if args.sums is not None:
                sha = sha256_for(args.sums.read_text(encoding="utf-8"), version)
            else:
                sha = sha256_file(args.zip)
            text = render(version, sha, url=args.url or DEFAULT_URL)
            if args.output is None:
                sys.stdout.write(text)
            else:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(text, encoding="utf-8")
                sys.stdout.write(f"wrote {args.output}\n")
            return 0

        version = final_version(args.version)
        sha = sha256_for(args.sums.read_text(encoding="utf-8"), version)
        changed, message = bump(args.tap_dir, render(version, sha), version, dry_run=args.dry_run)
    except (CaskError, OSError) as e:
        sys.stderr.write(f"cask: {e}\n")
        return 1
    sys.stdout.write(message)
    line = f"changed={str(changed).lower()}\n"
    sys.stdout.write(line)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
