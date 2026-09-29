import io
import os
import shutil
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sombra.cli import build_parser, main
from sombra.privacy import commands, secrets
from sombra.privacy.control import ControlServer
from sombra.privacy.pause import PauseController

KEY = "sk-test-DO-NOT-PRINT-42"


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.store.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.store[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.store[(service_name, username)]


@pytest.fixture
def kr(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "default_backend", lambda: fake)
    return fake


def test_commands_registered() -> None:
    parser = build_parser()
    for argv in (
        ["auth", "set", "anthropic"],
        ["auth", "clear", "anthropic"],
        ["auth", "status", "anthropic"],
        ["retention", "run", "--dry-run"],
        ["delete", "m"],
        ["pause"],
        ["resume"],
    ):
        assert callable(parser.parse_args(argv).func)


def test_auth_set_prompts_hidden_and_never_prints_key(
    kr: FakeKeyring, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands.getpass, "getpass", lambda prompt: KEY)
    assert main(["auth", "set", "anthropic"]) == 0
    assert kr.store == {("sombra", "anthropic"): KEY}
    assert main(["auth", "status", "anthropic"]) == 0
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert main(["auth", "clear", "anthropic"]) == 0
    assert main(["auth", "clear", "anthropic"]) == 0
    assert main(["auth", "status", "anthropic"]) == 1
    assert "not set" in capsys.readouterr().out


def test_auth_set_from_stdin(kr: FakeKeyring) -> None:
    args = build_parser().parse_args(["auth", "set", "openai", "--stdin"])
    assert commands._auth_set(args, stdin=io.StringIO(KEY + "\n")) == 0
    assert kr.store[("sombra", "openai")] == KEY


def test_auth_errors(kr: FakeKeyring, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands.getpass, "getpass", lambda prompt: "")
    assert main(["auth", "set", "anthropic"]) == 1
    assert main(["auth", "clear", "bad name"]) == 1
    assert main(["auth", "status", "bad name"]) == 1


def _meeting(root: Path, name: str, age_days: float) -> Path:
    m = root / name
    (m / "frames").mkdir(parents=True)
    ts = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
    for f in (m / "transcript.md", m / "frames" / "f0001.jpg"):
        f.write_text("x")
        os.utime(f, (ts, ts))
    return m


def test_retention_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = _meeting(tmp_path, "old", 40)
    new = _meeting(tmp_path, "new", 1)
    assert main(["retention", "run", "--root", str(tmp_path), "--dry-run"]) == 0
    assert "would delete" in capsys.readouterr().out
    assert old.exists()
    assert main(["retention", "run", "--root", str(tmp_path)]) == 0
    assert not old.exists()
    assert new.exists()
    assert main(["retention", "run", "--root", str(tmp_path), "--frames-days", "0"]) == 0
    assert not (new / "frames" / "f0001.jpg").exists()


def test_retention_reports_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    assert main(["retention", "run", "--root", str(root)]) == 0
    assert "skipped" in capsys.readouterr().out


def test_retention_bad_args(tmp_path: Path) -> None:
    assert main(["retention", "run", "--root", str(tmp_path / "missing")]) == 1
    assert main(["retention", "run", "--root", str(tmp_path), "--text-days", "-1"]) == 1


def test_delete_asks_and_needs_exact_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = _meeting(tmp_path, "2026-09-29_1430_daily", 1)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    assert main(["delete", m.name, "--root", str(tmp_path)]) == 1
    assert m.exists()

    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert main(["delete", m.name, "--root", str(tmp_path)]) == 1
    assert m.exists()
    monkeypatch.setattr("builtins.input", lambda prompt: m.name)
    assert main(["delete", m.name, "--root", str(tmp_path)]) == 0
    assert not m.exists()


def test_delete_yes_and_refusals(tmp_path: Path) -> None:
    m = _meeting(tmp_path, "m", 1)
    assert main(["delete", "..", "--root", str(tmp_path), "--yes"]) == 1
    assert main(["delete", "missing", "--root", str(tmp_path), "--yes"]) == 1
    assert main(["delete", "m", "--root", str(tmp_path), "--yes"]) == 0
    assert not m.exists()


@pytest.fixture
def sock_path() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="sb", dir="/tmp"))
    try:
        yield d / "c.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def test_pause_resume_commands(sock_path: Path) -> None:
    import asyncio

    c = PauseController()
    async with ControlServer(c, sock_path):
        assert await asyncio.to_thread(main, ["pause", "--socket", str(sock_path)]) == 0
        assert c.is_paused
        assert await asyncio.to_thread(main, ["resume", "--socket", str(sock_path)]) == 0
        assert not c.is_paused


def test_pause_without_session(sock_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pause", "--socket", str(sock_path)]) == 1
    assert "no running Sombra session" in capsys.readouterr().err
