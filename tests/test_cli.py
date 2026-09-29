import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from sombra import __version__
from sombra.cli import build_parser, discover_command_modules, main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: sombra" in capsys.readouterr().out


@pytest.fixture
def fake_pkg(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "fakesombra"
    (root / "withcmd").mkdir(parents=True)
    (root / "nocmd").mkdir()
    (root / "__init__.py").write_text("")
    (root / "withcmd" / "__init__.py").write_text("")
    (root / "nocmd" / "__init__.py").write_text("")
    (root / "loose.py").write_text("")
    (root / "withcmd" / "commands.py").write_text(
        textwrap.dedent(
            """
            def register(subparsers):
                p = subparsers.add_parser("hello")
                p.add_argument("name")
                p.set_defaults(func=lambda args: 3 if args.name == "x" else 0)
            """
        )
    )
    sys.path.insert(0, str(tmp_path))
    try:
        yield "fakesombra"
    finally:
        sys.path.remove(str(tmp_path))
        for mod in [m for m in sys.modules if m.startswith("fakesombra")]:
            del sys.modules[mod]


def test_discovers_only_packages_with_commands(fake_pkg: str) -> None:
    assert discover_command_modules(fake_pkg) == ["fakesombra.withcmd.commands"]


def test_registered_command_runs(fake_pkg: str) -> None:
    args = build_parser(fake_pkg).parse_args(["hello", "x"])
    assert args.func(args) == 3
