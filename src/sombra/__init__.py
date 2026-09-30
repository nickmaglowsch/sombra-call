"""Sombra: an autonomous meeting participant that runs on your machine."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Set at build time from the git tag by hatch-vcs (see docs/release.md).
    __version__ = version("sombra")
except PackageNotFoundError:
    # A source tree that was never installed.
    __version__ = "0.0.0"  # pragma: no cover
