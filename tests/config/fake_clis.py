"""Fake ``claude`` / ``codex`` CLIs for the setup wizard and doctor (no real CLI runs).

Each CLI is absent (``None``) or a :class:`Cli` with a version and a login state. The
outputs mimic the real status commands (checked on the real CLIs, see
``sombra.config.providers``): ``claude auth status`` prints JSON and exits 1 when logged
out; ``codex login status`` prints one line, with a masked key for an API-key login.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sombra.config.providers import CLAUDE, CODEX, ProcResult

MASKED_KEY = "sk-proj-***ABCD"


@dataclass
class Cli:
    version: str
    login: str = "subscription"  # subscription | api-key | none | garbage


@dataclass
class FakeClis:
    claude: Cli | None = field(default_factory=lambda: Cli("2.1.285"))
    codex: Cli | None = field(default_factory=lambda: Cli("0.159.1"))
    install_works: bool = True
    login_works: bool = True
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)
    interactive: list[list[str]] = field(default_factory=list)

    def _cli(self, name: str) -> Cli | None:
        return self.claude if name == "claude" else self.codex if name == "codex" else None

    def which(self, name: str) -> str | None:
        return f"/opt/bin/{name}" if self._cli(name) is not None else None

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> ProcResult:
        self.calls.append((list(argv), dict(env)))
        name = Path(argv[0]).name
        cli = self._cli(name)
        if cli is None:
            return ProcResult(127, "", f"{name}: not found")
        args = list(argv[1:])
        if args == ["--version"]:
            text = (
                f"{cli.version} (Claude Code)" if name == "claude" else f"codex-cli {cli.version}"
            )
            return ProcResult(0, text + "\n", "")
        if name == "claude" and args == list(CLAUDE.status):
            return _claude_status(cli.login)
        if name == "codex" and args == list(CODEX.status):
            return _codex_status(cli.login)
        return ProcResult(2, "", f"unexpected {args}")

    def run_interactive(self, argv: Sequence[str]) -> int:
        argv = list(argv)
        self.interactive.append(argv)
        if argv[:2] == ["sh", "-c"]:
            line = argv[2]
            spec = CLAUDE if "claude" in line else CODEX
            new = Cli(".".join(map(str, spec.min_version)), "none")
            if self.install_works:
                if spec is CLAUDE:
                    old = self.claude
                    self.claude = Cli(new.version, old.login if old else "none")
                else:
                    old = self.codex
                    self.codex = Cli(new.version, old.login if old else "none")
            return 0
        cli = self._cli(Path(argv[0]).name)
        if cli is not None and self.login_works:
            cli.login = "subscription"
        return 0


def _claude_status(login: str) -> ProcResult:
    base = {"apiProvider": "firstParty", "configDirectory": "/home/u/.claude"}
    if login == "garbage":
        return ProcResult(3, "something went wrong\n", "")
    if login == "none":
        return ProcResult(1, json.dumps({"loggedIn": False, **base}), "")
    if login == "api-key":
        data = {"loggedIn": True, "authMethod": "api_key", "apiKeySource": "apiKeyHelper"}
        return ProcResult(0, json.dumps({**data, **base}), "")
    return ProcResult(0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", **base}), "")


def _codex_status(login: str) -> ProcResult:
    if login == "garbage":
        return ProcResult(3, "", "error: could not read auth.json\n")
    if login == "none":
        return ProcResult(1, "", "Not logged in\n")
    if login == "api-key":
        return ProcResult(0, "", f"Logged in using an API key - {MASKED_KEY}\n")
    return ProcResult(0, "", "Logged in using ChatGPT\n")
