"""Read-only tools the agent gets on the meeting folder (PRD C2).

The agent may only ``read``, ``grep`` and ``glob`` inside the meeting folder, and
``view_frame`` a kept screenshot by its ``TELA`` id. There is no shell, no write
and no network tool. Every path is resolved and must stay under the meeting
folder: absolute paths, ``~``, ``..`` components and symlinks that point outside
are refused.

The tools are backend-agnostic: :meth:`MeetingTools.specs` returns JSON-schema
tool definitions (Anthropic Messages shape, which other backends can translate)
and :meth:`MeetingTools.run` returns content blocks for a ``tool_result``.
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from sombra.contracts.timeline import FRAME_ID_RE

TOOL_NAMES = ("glob", "grep", "read", "view_frame")

_MAX_PATTERN_CHARS = 200


class ToolError(Exception):
    """A tool call the agent made was refused or failed; sent back as ``is_error``."""


class MeetingTools:
    """Read-only access to one meeting folder."""

    def __init__(
        self,
        meeting_dir: Path,
        *,
        max_read_chars: int = 40_000,
        max_matches: int = 200,
        max_glob_results: int = 500,
    ) -> None:
        self.root = meeting_dir.resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(str(meeting_dir))
        self.max_read_chars = max_read_chars
        self.max_matches = max_matches
        self.max_glob_results = max_glob_results

    # --- path confinement ------------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """Resolve a path the agent gave, relative to the meeting folder, or raise."""
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path must be a non-empty string relative to the meeting folder")
        if "\x00" in rel:
            raise ToolError("invalid path")
        norm = rel.replace("\\", "/")
        pure = PurePosixPath(norm)
        if pure.is_absolute() or norm.startswith("~") or re.match(r"^[A-Za-z]:", norm):
            raise ToolError(f"absolute paths are not allowed: {rel!r}")
        if ".." in pure.parts:
            raise ToolError(f"'..' is not allowed in paths: {rel!r}")
        try:
            resolved = (self.root / pure).resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            raise ToolError(f"not found: {rel}") from None
        except (OSError, RuntimeError):  # symlink loops and friends
            raise ToolError(f"cannot resolve: {rel}") from None
        if not self._inside(resolved):
            raise ToolError(f"path leaves the meeting folder: {rel!r}")
        return resolved

    def _inside(self, path: Path) -> bool:
        return path == self.root or path.is_relative_to(self.root)

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def _walk_files(self, start: Path) -> Iterator[Path]:
        """Files under ``start`` (sorted, deterministic) whose real path stays inside."""
        if start.is_file():
            yield start
            return
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                candidate = Path(dirpath) / name
                try:
                    real = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if self._inside(real) and real.is_file():
                    yield real

    def _glob_files(self, pattern: str) -> Iterator[Path]:
        """Real paths of files matching ``pattern`` that stay inside the folder."""
        _check_glob(pattern)
        try:
            candidates = sorted(self.root.glob(pattern.replace("\\", "/")))
        except (ValueError, NotImplementedError) as e:
            raise ToolError(f"invalid glob pattern: {e}") from None
        for candidate in candidates:
            try:
                real = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if self._inside(real) and real.is_file():
                yield real

    # --- tools -----------------------------------------------------------------------

    def read(self, path: str, offset: int = 0, limit: int | None = None) -> str:
        """Text of a file, as numbered lines starting at line ``offset + 1``."""
        target = self.resolve(path)
        if target.is_dir():
            raise ToolError(f"{path} is a directory; use glob to list it")
        data = target.read_bytes()
        if b"\x00" in data[:4096]:
            raise ToolError(f"{path} is a binary file; use view_frame for screenshots")
        lines = data.decode("utf-8", errors="replace").splitlines()
        offset = max(0, int(offset))
        end = len(lines) if limit is None else offset + max(0, int(limit))
        out: list[str] = []
        size = 0
        for n, line in enumerate(lines[offset:end], start=offset + 1):
            row = f"{n:6d}\t{line}"
            size += len(row) + 1
            if size > self.max_read_chars:
                out.append(f"[truncated at line {n - 1}; call read again with offset={n - 1}]")
                break
            out.append(row)
        return "\n".join(out) if out else "(no lines in range)"

    def grep(
        self, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False
    ) -> str:
        """Lines matching a regular expression, as ``path:line: text``."""
        if len(pattern) > _MAX_PATTERN_CHARS:
            raise ToolError(f"pattern longer than {_MAX_PATTERN_CHARS} characters")
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"invalid regular expression: {e}") from None
        allowed = None if glob is None else set(self._glob_files(glob))
        start = self.resolve(path)
        hits: list[str] = []
        for file in self._walk_files(start):
            rel = self._rel(file)
            if allowed is not None and file not in allowed:
                continue
            data = file.read_bytes()
            if b"\x00" in data[:4096]:
                continue  # binary (frames)
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{rel}:{n}: {line[:500]}")
                    if len(hits) >= self.max_matches:
                        hits.append(f"[stopped at {self.max_matches} matches]")
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "(no matches)"

    def glob(self, pattern: str) -> str:
        """Paths (relative to the meeting folder) matching a glob pattern, sorted."""
        found = sorted({self._rel(f) for f in self._glob_files(pattern)})
        if len(found) > self.max_glob_results:
            extra = len(found) - self.max_glob_results
            found = [*found[: self.max_glob_results], f"[{extra} more not shown]"]
        return "\n".join(found) if found else "(no files)"

    def frame_path(self, frame_id: str) -> Path:
        """The JPEG for a ``TELA`` frame id, served only from ``frames/``."""
        if not isinstance(frame_id, str) or not FRAME_ID_RE.match(frame_id):
            raise ToolError(f"invalid frame id: {frame_id!r} (expected e.g. f0123)")
        frames = self.root / "frames"
        target = self.resolve(f"frames/{frame_id}.jpg")
        if not target.is_relative_to(frames.resolve()):
            raise ToolError(f"frame {frame_id} is not in frames/")
        return target

    def view_frame(self, frame_id: str) -> list[dict[str, Any]]:
        data = self.frame_path(frame_id).read_bytes()
        return [
            {"type": "text", "text": f"TELA {frame_id}:"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            },
        ]

    # --- dispatch --------------------------------------------------------------------

    def run(self, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Run one tool call; return ``tool_result`` content blocks. Raises :class:`ToolError`."""
        if not isinstance(args, dict):
            raise ToolError("tool input must be an object")
        try:
            if name == "read":
                text = self.read(
                    _str(args, "path"), int(args.get("offset", 0)), _opt_int(args, "limit")
                )
            elif name == "grep":
                text = self.grep(
                    _str(args, "pattern"),
                    str(args.get("path", ".")),
                    _opt_str(args, "glob"),
                    bool(args.get("ignore_case", False)),
                )
            elif name == "glob":
                text = self.glob(_str(args, "pattern"))
            elif name == "view_frame":
                return self.view_frame(_str(args, "frame_id"))
            else:
                raise ToolError(f"unknown tool {name!r}; available: {', '.join(TOOL_NAMES)}")
        except (TypeError, ValueError) as e:
            raise ToolError(f"bad arguments for {name}: {e}") from None
        return [{"type": "text", "text": text}]

    @staticmethod
    def specs() -> list[dict[str, Any]]:
        """Tool definitions, sorted by name so the rendered prefix is byte-stable."""
        return [dict(spec) for spec in _SPECS]


def _check_glob(pattern: str) -> None:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolError("glob pattern must be a non-empty string")
    norm = pattern.replace("\\", "/")
    if norm.startswith(("/", "~")) or ".." in PurePosixPath(norm).parts:
        raise ToolError(f"glob patterns must stay inside the meeting folder: {pattern!r}")


def _str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"missing string argument {key!r}")
    return value


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) and value else None


def _opt_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    return None if value is None else int(value)


_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "glob",
        "description": (
            "List files in the meeting folder whose path matches a glob pattern, e.g. "
            "'context/**/*.md' or 'frames/*.jpg'. Paths are relative to the meeting folder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "grep",
        "description": (
            "Search file contents in the meeting folder with a regular expression. Returns "
            "'path:line: text'. Optional 'path' (file or folder, default '.') and 'glob' filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "ignore_case": {"type": "boolean"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read",
        "description": (
            "Read a text file in the meeting folder (e.g. 'transcript.md', 'context/notes.md') "
            "as numbered lines. Optional 'offset' (lines to skip) and 'limit' (max lines)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "view_frame",
        "description": (
            "Look at a screenshot by the id in a 'TELA fNNNN' timeline marker, e.g. 'f0123'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"frame_id": {"type": "string", "pattern": "^f[0-9]{4,}$"}},
            "required": ["frame_id"],
            "additionalProperties": False,
        },
    },
)
