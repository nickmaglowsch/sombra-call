"""Release tags: parse ``vX.Y.Z[-rcN|-betaN|-alphaN]`` and check the built artifacts match.

The version of a release comes only from its git tag (hatch-vcs, see docs/release.md), so
this is the one place that knows the tag grammar::

    python scripts/release/tags.py check v0.2.0-rc1 --dist dist [--github-output "$GITHUB_OUTPUT"]

``check`` fails when the tag is malformed or when ``dist/`` holds a wheel or sdist whose
version is not the tag's. It prints ``version=…`` and ``prerelease=true|false`` (plus
``latest=true|false`` given ``--tags <every tag>``) and, with ``--github-output``, appends
the same lines there for later workflow steps.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "sombra"

_TAG = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>alpha|beta|rc)\.?(?P<pre_n>0|[1-9]\d*))?$"
)
_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}
_PRE_ORDER = {"alpha": 0, "beta": 1, "rc": 2}


class TagError(ValueError):
    """The tag is not a release tag, or the artifacts don't carry its version."""


@dataclass(frozen=True)
class ReleaseTag:
    name: str
    major: int
    minor: int
    patch: int
    pre: str | None = None
    pre_n: int = 0

    @property
    def prerelease(self) -> bool:
        return self.pre is not None

    @property
    def version(self) -> str:
        """The PEP 440 version hatch-vcs builds from this tag: ``v0.2.0-rc1`` → ``0.2.0rc1``."""
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.pre is None else f"{base}{_PEP440_PRE[self.pre]}{self.pre_n}"

    @property
    def sort_key(self) -> tuple[int, int, int, int, int]:
        # A final release sorts after all of its pre-releases.
        pre_rank = 3 if self.pre is None else _PRE_ORDER[self.pre]
        return (self.major, self.minor, self.patch, pre_rank, self.pre_n)


def parse_tag(name: str) -> ReleaseTag:
    m = _TAG.match(name.strip())
    if m is None:
        raise TagError(
            f"{name!r} is not a release tag; expected vX.Y.Z or vX.Y.Z-rcN / -betaN / -alphaN"
        )
    return ReleaseTag(
        name=name.strip(),
        major=int(m["major"]),
        minor=int(m["minor"]),
        patch=int(m["patch"]),
        pre=m["pre"],
        pre_n=int(m["pre_n"] or 0),
    )


def release_tags(names: Iterable[str]) -> list[ReleaseTag]:
    """The names that are release tags, oldest first. Other tags are ignored."""
    found = []
    for name in names:
        try:
            found.append(parse_tag(name))
        except TagError:
            continue
    return sorted(found, key=lambda t: t.sort_key)


def previous_tag(current: ReleaseTag, names: Iterable[str]) -> ReleaseTag | None:
    """The release the notes for ``current`` start from.

    A final release starts from the previous *final* release, so ``v0.3.0`` lists
    everything since ``v0.2.0`` even if ``v0.3.0-rc1`` came in between. A pre-release
    starts from the tag right before it, pre-release or not.
    """
    earlier = [t for t in release_tags(names) if t.sort_key < current.sort_key]
    if not current.prerelease:
        earlier = [t for t in earlier if not t.prerelease]
    return earlier[-1] if earlier else None


def is_latest(current: ReleaseTag, names: Iterable[str]) -> bool:
    """Whether ``current`` should be the repo's "Latest" release (what ``releases/latest``
    and so ``install.sh`` resolve): a final release no lower than any other final release.
    A patch to an older line (``v0.2.1`` after ``v0.3.0``) is not."""
    if current.prerelease:
        return False
    finals = [t for t in release_tags(names) if not t.prerelease]
    return all(t.sort_key <= current.sort_key for t in finals)


def artifact_versions(dist: Path) -> dict[str, str]:
    """``{file name: version}`` for every sombra wheel and sdist in ``dist``."""
    found = {}
    for p in sorted(dist.iterdir()):
        if p.name.endswith(".whl"):
            parts = p.name.split("-")
            if len(parts) >= 5 and parts[0] == PACKAGE:
                found[p.name] = parts[1]
        elif p.name.startswith(f"{PACKAGE}-") and p.name.endswith(".tar.gz"):
            found[p.name] = p.name.removeprefix(f"{PACKAGE}-").removesuffix(".tar.gz")
    return found


def check_artifacts(tag: ReleaseTag, dist: Path) -> list[str]:
    """Names of the wheel and sdist in ``dist``; raises ``TagError`` if either is off."""
    versions = artifact_versions(dist)
    kinds = {"wheel": any(n.endswith(".whl") for n in versions)}
    kinds["sdist"] = any(n.endswith(".tar.gz") for n in versions)
    problems = [f"no {kind} in {dist}" for kind, ok in kinds.items() if not ok]
    problems += [
        f"{name} has version {v}, tag {tag.name} means {tag.version}"
        for name, v in versions.items()
        if v != tag.version
    ]
    if problems:
        raise TagError("; ".join(problems))
    return sorted(versions)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("check", help="validate a tag and the artifacts built from it")
    check.add_argument("tag")
    check.add_argument("--dist", type=Path, help="directory with the built wheel and sdist")
    check.add_argument("--github-output", type=Path, help="append key=value lines here")
    check.add_argument(
        "--tags",
        nargs="*",
        default=None,
        help="every release tag in the repo; adds latest=true|false to the output",
    )
    args = parser.parse_args(argv)
    try:
        tag = parse_tag(args.tag)
        if args.dist is not None:
            check_artifacts(tag, args.dist)
    except TagError as e:
        sys.stderr.write(f"tags: {e}\n")
        return 1
    lines = f"version={tag.version}\nprerelease={str(tag.prerelease).lower()}\n"
    if args.tags is not None:
        lines += f"latest={str(is_latest(tag, [*args.tags, tag.name])).lower()}\n"
    sys.stdout.write(lines)
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as f:
            f.write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
