"""Release notes from the titles of the PRs merged since the previous release tag.

``main`` only takes squash merges, so each commit on its first-parent history is one PR,
with a subject like ``[brain] Codex backend (#39)``. The notes list those titles::

    python scripts/release/notes.py v0.2.0 --repo owner/name --output notes.md

The range starts at the previous release tag (``tags.previous_tag``: a final release
starts from the previous final release) and covers the whole history for the first one.

Standard library only.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from tags import ReleaseTag, TagError, parse_tag, previous_tag

_PR_SUBJECT = re.compile(r"^(?P<title>.+?)\s*\(#(?P<number>\d+)\)$")


def _git(*args: str, cwd: Path | None = None) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git not found on PATH")
    out = subprocess.run(  # noqa: S603 - fixed argv built here, no shell
        [git, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return out.stdout


def git_tags(cwd: Path | None = None) -> list[str]:
    return _git("tag", "--list", "v*", cwd=cwd).split()


def git_subjects(tag: str, since: str | None, cwd: Path | None = None) -> list[str]:
    """Commit subjects on the first-parent history of ``tag``, newest first."""
    rev = tag if since is None else f"{since}..{tag}"
    out = _git("log", "--first-parent", "--format=%s", rev, "--", cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def render(
    tag: ReleaseTag, since: ReleaseTag | None, subjects: Sequence[str], repo: str | None = None
) -> str:
    prs, other = [], []
    for subject in subjects:
        m = _PR_SUBJECT.match(subject.strip())
        if m:
            prs.append(f"- {m['title']} (#{m['number']})")
        else:
            other.append(f"- {subject.strip()}")
    lines = []
    if tag.prerelease:
        lines += [f"Pre-release {tag.version}: for testing, not for everyday use.", ""]
    lines += ["## What's changed", ""]
    lines += prs or ["- No pull requests merged since the previous release."]
    if other:
        lines += ["", "### Other commits", "", *other]
    lines += ["", "## Verify the download", ""]
    lines += [
        "Download `SHA256SUMS` next to the assets and run `sha256sum -c SHA256SUMS`",
        "(Linux) or `shasum -a 256 -c SHA256SUMS` (macOS). See docs/release.md.",
    ]
    if repo:
        base = f"https://github.com/{repo}"
        link = (
            f"{base}/compare/{since.name}...{tag.name}" if since else f"{base}/commits/{tag.name}"
        )
        lines += ["", f"**Full changelog**: {link}"]
    return "\n".join(lines) + "\n"


def build_notes(tag_name: str, repo: str | None = None, cwd: Path | None = None) -> str:
    tag = parse_tag(tag_name)
    since = previous_tag(tag, git_tags(cwd))
    subjects = git_subjects(tag.name, since.name if since else None, cwd)
    return render(tag, since, subjects, repo)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag")
    parser.add_argument("--repo", help="owner/name, for the full-changelog link")
    parser.add_argument("--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    try:
        notes = build_notes(args.tag, args.repo)
    except (TagError, subprocess.CalledProcessError) as e:
        detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else e
        sys.stderr.write(f"notes: {detail}\n")
        return 1
    if args.output is None:
        sys.stdout.write(notes)
    else:
        args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
