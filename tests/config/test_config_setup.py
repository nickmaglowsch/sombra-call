"""``sombra setup`` with fake CLIs, a dict keychain and a fake smoke answer (#47)."""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fake_clis import MASKED_KEY, Cli, FakeClis

from sombra.cli import build_parser
from sombra.config import BrainConfig, ConfigError, UserConfig, load_user_config
from sombra.config import commands as config_commands
from sombra.config import setup as setup_mod
from sombra.config.commands import DEFAULT_CONFIG_TOML
from sombra.config.setup import SetupError, SetupIO, SetupRequest, run_setup, write_provider

SECRET_ENV = {
    "PATH": "/opt/bin",
    "HOME": "/home/u",
    "ANTHROPIC_API_KEY": "sk-ant-shell",
    "OPENAI_API_KEY": "sk-openai-shell",
    "CODEX_API_KEY": "sk-codex-shell",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-shell",
}

USER_CONFIG = """\
# my Sombra config
meetings_root = "~/Reunioes"   # moved to my Dropbox
autonomy_level = "L2"

[user]
name = "Nick"                  # people call me this
aliases = ["Nico"]

[brain]
backend = "claude"             # the old name
"""


@dataclass
class Machine:
    """The fake machine: CLIs, a keychain, scripted answers, captured output."""

    clis: FakeClis = field(default_factory=FakeClis)
    keys: set[str] = field(default_factory=set)
    answers: list[str] = field(default_factory=list)
    smoke_error: Exception | None = None
    out: io.StringIO = field(default_factory=io.StringIO)
    prompts: list[str] = field(default_factory=list)
    set_key_calls: list[str] = field(default_factory=list)
    smoked: list[UserConfig] = field(default_factory=list)
    store_on_set: bool = True

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def set_key(self, provider: str) -> int:  # stands in for `sombra auth set <provider>`
        self.set_key_calls.append(provider)
        if self.store_on_set:
            self.keys.add(provider)
        return 0

    def smoke(self, cfg: UserConfig) -> str:
        self.smoked.append(cfg)
        if self.smoke_error is not None:
            raise self.smoke_error
        return "pronto\n"

    def io(self) -> SetupIO:
        ticks = iter(range(100))
        return SetupIO(
            out=self.out,
            ask=self.ask,
            which=self.clis.which,
            run=self.clis.run,
            run_interactive=self.clis.run_interactive,
            has_key=lambda provider: provider in self.keys,
            set_key=self.set_key,
            smoke=self.smoke,
            environ=SECRET_ENV,
            clock=lambda: float(next(ticks)) * 1.5,
        )

    @property
    def text(self) -> str:
        return self.out.getvalue()


def _setup(path: Path, m: Machine, **kw: object) -> int:
    return run_setup(SetupRequest(config=path, **kw), m.io())  # type: ignore[arg-type]


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    path = tmp_path / "sombra" / "config.toml"
    path.parent.mkdir()
    path.write_text(USER_CONFIG, encoding="utf-8")
    return path


# --- the config file ---------------------------------------------------------------------------


def test_write_provider_keeps_every_other_key_and_comment(cfg_path: Path) -> None:
    write_provider(cfg_path, "codex", "api-key", "claude-code")
    text = cfg_path.read_text(encoding="utf-8")
    for line in USER_CONFIG.splitlines():
        if not line.startswith("backend"):
            assert line in text  # comments, blank lines and other keys untouched
    assert 'backend = "codex"             # the old name' in text  # value swapped in place
    cfg = load_user_config(cfg_path)
    assert cfg.brain == BrainConfig("codex", "api-key")
    assert cfg.summary.backend == "claude-code"
    assert cfg.user.name == "Nick" and cfg.meetings_root == Path("~/Reunioes").expanduser()

    write_provider(cfg_path, "claude-code", None, "follow")  # auth dropped, summary kept
    cfg = load_user_config(cfg_path)
    assert cfg.brain == BrainConfig("claude-code") and cfg.summary.backend == "follow"
    assert "auth" not in cfg_path.read_text(encoding="utf-8")
    assert "# my Sombra config" in cfg_path.read_text(encoding="utf-8")


def test_write_provider_round_trips_the_default_file(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.toml"
    write_provider(path, "claude-code", None, "follow")
    text = path.read_text(encoding="utf-8")
    assert text == DEFAULT_CONFIG_TOML.replace(
        'backend = "claude-api"                # claude-code',
        'backend = "claude-code"                # claude-code',
    )
    assert load_user_config(path).brain.backend == "claude-code"
    assert not list(path.parent.glob(".*sombra-setup"))  # no temp file left behind


def test_write_provider_adds_summary_only_when_not_following(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[user]\nname = "Ana"\n', encoding="utf-8")
    write_provider(path, "claude-code", None, "follow")
    assert "[summary]" not in path.read_text(encoding="utf-8")
    write_provider(path, "claude-code", None, "none")
    assert load_user_config(path).summary.resolve(load_user_config(path).brain) is None


def test_write_provider_refuses_a_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("autonomy_level = 'L9'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="autonomy_level"):
        write_provider(path, "claude-code", None, "follow")
    assert path.read_text(encoding="utf-8") == "autonomy_level = 'L9'\n"


# --- arguments ---------------------------------------------------------------------------------


def _args(*argv: str) -> SetupRequest:
    return SetupRequest.from_args(build_parser().parse_args(["setup", *argv]))


def test_arguments() -> None:
    req = _args("--non-interactive", "--backend", "codex", "--auth", "api-key", "--no-smoke")
    assert (req.backend, req.auth, req.interactive, req.smoke) == ("codex", "api-key", False, False)
    assert _args("--show").show and _args().interactive
    with pytest.raises(SetupError, match="codex only"):
        _args("--backend", "claude-code", "--auth", "api-key")
    with pytest.raises(SetupError, match="needs --backend"):
        _args("--non-interactive")


def test_setup_command_wiring(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sombra.cli import main

    m = Machine()
    monkeypatch.setattr(setup_mod, "live_io", m.io)
    argv = ["setup", "--non-interactive", "--backend", "claude-code", "--config", str(cfg_path)]
    assert main(argv) == 0
    assert load_user_config(cfg_path).brain.backend == "claude-code"
    assert main(["setup", "--non-interactive"]) == 2
    assert "needs --backend" in capsys.readouterr().err
    assert config_commands._setup  # registered through config.commands


# --- the wizard, interactive -------------------------------------------------------------------


def test_claude_subscription_ready(cfg_path: Path) -> None:
    m = Machine(answers=["1", ""])  # Claude on the subscription; summaries follow
    assert _setup(cfg_path, m) == 0
    cfg = load_user_config(cfg_path)
    assert cfg.brain == BrainConfig("claude-code") and cfg.summary.backend == "follow"
    assert m.smoked and m.smoked[0].brain.backend == "claude-code"
    assert "ok  Claude Code 2.1.285" in m.text and "logged in (subscription)" in m.text
    assert "answered in 1.5 s: pronto" in m.text
    assert m.clis.interactive == []  # nothing installed, nobody logged in


def test_status_commands_never_see_a_key_or_token(cfg_path: Path) -> None:
    m = Machine(answers=["3", ""])  # Codex on ChatGPT
    assert _setup(cfg_path, m) == 0
    assert m.clis.calls
    for argv, env in m.clis.calls:
        assert set(env) <= {"PATH", "HOME"}, argv  # the allowlist, no key, no token
    assert MASKED_KEY not in m.text


def test_missing_cli_installed_with_consent_then_logged_in(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(claude=None), answers=["1", "", "y", "y"])
    assert _setup(cfg_path, m) == 0
    install, login = m.clis.interactive
    assert install == ["sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]
    assert login == ["/opt/bin/claude", "auth", "login"]  # the CLI's own flow, no token here
    assert "Official install command" in m.text


def test_missing_cli_declined_writes_the_choice_and_says_what_is_left(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(codex=None), answers=["3", "", "n"])
    assert _setup(cfg_path, m) == 1
    assert m.clis.interactive == [] and m.smoked == []
    assert load_user_config(cfg_path).brain == BrainConfig("codex", "subscription")
    assert "npm install -g @openai/codex@0.159.1" in m.text and "Left to do" in m.text


def test_install_that_does_not_land_on_path(cfg_path: Path) -> None:
    clis = FakeClis(claude=None, install_works=False)
    m = Machine(clis=clis, answers=["1", "", "y"])
    assert _setup(cfg_path, m) == 1
    assert "open a new terminal" in m.text


@pytest.mark.parametrize(("answer", "code"), [("y", 0), ("n", 1)])
def test_old_cli_update_offered(cfg_path: Path, answer: str, code: int) -> None:
    m = Machine(clis=FakeClis(claude=Cli("2.1.100")), answers=["1", "", answer])
    assert _setup(cfg_path, m) == code
    assert "2.1.100; Sombra needs 2.1.285 or newer" in m.text
    if answer == "y":
        assert m.clis.interactive == [["sh", "-c", "claude update"]]
    else:
        assert "`claude update`" in m.text and m.clis.interactive == []


def test_codex_newer_than_verified_is_pinned_back(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(codex=Cli("0.161.0")), answers=["3", "", "y"])
    assert _setup(cfg_path, m) == 0
    assert "newer than 0.159.1" in m.text
    assert m.clis.interactive == [["sh", "-c", "npm install -g @openai/codex@0.159.1"]]


@pytest.mark.parametrize(("answer", "code"), [("y", 0), ("", 1)])
def test_logged_out_cli_offers_its_own_login(cfg_path: Path, answer: str, code: int) -> None:
    m = Machine(clis=FakeClis(claude=Cli("2.1.290", "none")), answers=["1", "", answer])
    assert _setup(cfg_path, m) == code
    assert "Sombra never sees the token" in m.prompts[2]
    if code:
        assert "`claude auth login`" in m.text and m.smoked == []


def test_login_that_fails_is_reported(cfg_path: Path) -> None:
    clis = FakeClis(codex=Cli("0.159.1", "none"), login_works=False)
    m = Machine(clis=clis, answers=["3", "", "y"])
    assert _setup(cfg_path, m) == 1
    assert "codex login" in m.text and "--device-auth" in m.text


def test_subscription_logged_in_with_an_api_key_is_flagged(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(claude=Cli("2.1.285", "api-key")), answers=["1", ""])
    assert _setup(cfg_path, m) == 0
    assert "billed, not your subscription" in m.text


def test_api_key_mode_delegates_to_sombra_auth_set(cfg_path: Path) -> None:
    m = Machine(answers=["2", "", ""])  # Claude API; summaries follow; store the key: yes
    assert _setup(cfg_path, m) == 0
    assert m.set_key_calls == ["anthropic"]
    assert m.clis.interactive == [] and m.clis.calls == []  # no CLI needed for the API
    assert load_user_config(cfg_path).brain == BrainConfig("claude-api")


def test_api_key_declined(cfg_path: Path) -> None:
    m = Machine(answers=["2", "", "n"])
    assert _setup(cfg_path, m) == 1
    assert m.set_key_calls == [] and "`sombra auth set anthropic`" in m.text


def test_codex_api_key_checks_the_key_and_the_login_for_summaries(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(codex=Cli("0.159.1", "none")), answers=["4", "", "", "n"])
    assert _setup(cfg_path, m) == 1
    assert m.set_key_calls == ["openai"]
    assert "summaries never get the API key" in m.prompts[1]
    assert "Summaries: codex" in m.text and "`codex login" in m.text
    assert load_user_config(cfg_path).brain == BrainConfig("codex", "api-key")


def test_summaries_on_another_backend(cfg_path: Path) -> None:
    m = Machine(keys={"anthropic"}, answers=["1", "n", "2"])  # agent claude-code; summaries API
    assert _setup(cfg_path, m) == 0
    cfg = load_user_config(cfg_path)
    assert cfg.brain.backend == "claude-code" and cfg.summary.backend == "claude-api"
    assert "Summaries: claude-api" in m.text


def test_enter_keeps_the_current_choice(cfg_path: Path) -> None:
    write_provider(cfg_path, "codex", "api-key", "follow")
    m = Machine(keys={"openai"}, answers=["", "", "x", "9", ""])
    assert _setup(cfg_path, m, smoke=False) == 0
    assert load_user_config(cfg_path).brain == BrainConfig("codex", "api-key")


def test_bad_menu_answers_are_asked_again(cfg_path: Path) -> None:
    m = Machine(answers=["x", "9", "1", ""])
    assert _setup(cfg_path, m) == 0
    assert m.text.count("type a number from 1 to 4") == 2


def test_eof_on_every_prompt_means_defaults_and_no(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(claude=None))  # no answers at all: EOF
    assert _setup(cfg_path, m) == 1  # default choice (the current claude-api), key: no
    assert m.set_key_calls == [] and m.clis.interactive == []


def test_smoke_failure_keeps_the_choice(cfg_path: Path) -> None:
    m = Machine(answers=["1", ""], smoke_error=RuntimeError("Claude Code is not logged in"))
    assert _setup(cfg_path, m) == 1
    assert "did not answer (RuntimeError): Claude Code is not logged in" in m.text
    assert load_user_config(cfg_path).brain.backend == "claude-code"


def test_broken_config_is_not_touched(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[brain]\nbackend = 'gpt'\n", encoding="utf-8")
    m = Machine(answers=["1", ""])
    assert _setup(path, m) == 2
    assert "fix the config first" in m.text and "brain.backend" in m.text
    assert path.read_text(encoding="utf-8") == "[brain]\nbackend = 'gpt'\n"


def test_unwritable_config_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a: object, **k: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(setup_mod, "write_provider", fail)
    m = Machine(answers=["1", ""])
    assert _setup(tmp_path / "config.toml", m) == 2 and "could not write" in m.text


# --- non-interactive and --show ------------------------------------------------------------------


def _never_ask(prompt: str) -> str:
    raise AssertionError(f"prompted in --non-interactive: {prompt}")


def _quiet(m: Machine) -> Callable[[], SetupIO]:
    def make() -> SetupIO:
        io_ = m.io()
        io_.ask = _never_ask
        return io_

    return make


def test_non_interactive_ready(cfg_path: Path) -> None:
    m = Machine()
    req = SetupRequest(cfg_path, backend="codex", interactive=False)
    assert run_setup(req, _quiet(m)()) == 0
    assert load_user_config(cfg_path).brain == BrainConfig("codex", "subscription")
    assert m.smoked


def test_non_interactive_never_installs_or_logs_in(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(claude=None, codex=Cli("0.159.1", "none")))
    for backend in ("claude-code", "codex"):
        req = SetupRequest(cfg_path, backend=backend, interactive=False)
        assert run_setup(req, _quiet(m)()) == 1
    assert m.clis.interactive == [] and m.smoked == []


def test_non_interactive_summary_backend_and_no_smoke(cfg_path: Path) -> None:
    m = Machine(keys={"anthropic"})
    req = SetupRequest(
        cfg_path, backend="claude-code", summary="claude-api", interactive=False, smoke=False
    )
    assert run_setup(req, _quiet(m)()) == 0
    assert m.smoked == []
    assert load_user_config(cfg_path).summary.backend == "claude-api"


def test_show(cfg_path: Path) -> None:
    m = Machine(clis=FakeClis(claude=None, codex=Cli("0.160.2", "api-key")))
    assert _setup(cfg_path, m, show=True) == 0
    assert "agent:     claude-api" in m.text
    assert "summaries: claude-api (follows the agent)" in m.text
    assert "claude:    not installed" in m.text  # values line up at column 11
    assert "codex:     0.160.2, login: api-key" in m.text
    assert MASKED_KEY not in m.text
    assert cfg_path.read_text(encoding="utf-8") == USER_CONFIG  # --show writes nothing


def test_show_bad_config(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("colour = 1\n", encoding="utf-8")
    m = Machine()
    assert _setup(path, m, show=True) == 2 and "colour" in m.text
