"""``sombra doctor`` checks against fake probes (no OS calls)."""

import hashlib
import json
import plistlib
from dataclasses import replace
from pathlib import Path

import pytest
from fake_clis import MASKED_KEY, Cli, FakeClis

from sombra.config import BrainConfig, doctor
from sombra.config.doctor import (
    Check,
    ModelSpec,
    Permission,
    Probes,
    Status,
    TccHolder,
    app_bundle_of,
    exit_code,
    holder_from_path,
    host_app,
    parse_signature,
    read_os_release,
    render_json,
    render_text,
    run_checks,
    running_app,
)

GIB = 1 << 30
WHISPER = b"whisper"
VAD = b"vad"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _probes(tmp_path: Path, **kw: object) -> Probes:
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    (models / "ggml-w.bin").write_bytes(WHISPER)
    (models / "vad.onnx").write_bytes(VAD)
    base = Probes(
        platform="darwin",
        os_version="14.5",
        os_name="macOS",
        machine="arm64",
        sombra_version="0.1.0",
        python_version="3.12.7",
        models_dir=models,
        models=(
            ModelSpec("whisper w", "ggml-w.bin", _sha(WHISPER)),
            ModelSpec("silero-vad", "vad.onnx", _sha(VAD)),
        ),
        data_dir=tmp_path / "Sombra" / "meetings",
        host_app="iTerm",
        sha256=doctor.file_sha256,
        disk_free=lambda path: 50 * GIB,
        which=lambda name: f"/usr/local/bin/{name}",
        has_key=lambda provider: True,
        mic_permission=lambda: Permission.GRANTED,
        screen_permission=lambda: Permission.GRANTED,
        accessibility_permission=lambda: Permission.GRANTED,
        default_input=lambda: "MacBook Pro Microphone",
        system_audio=lambda: "tap",
        tcc_holder=TccHolder("iTerm", "com.googlecode.iterm2", "/Applications/iTerm.app"),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_all_green_mac(tmp_path: Path) -> None:
    checks = run_checks(_probes(tmp_path))
    assert [c for c in checks if c.status is not Status.OK] == []
    assert exit_code(checks) == 0
    sections = [c.section for c in checks]
    assert sections == sorted(sections, key=["sombra", "macos", "agent"].index)
    names = _by_name(checks)
    assert names["microphone"].detail == "granted to iTerm"
    assert names["system audio"].fix  # tells which app is asked for System Audio Recording


def test_missing_model_fails_with_download_fix(tmp_path: Path) -> None:
    p = _probes(tmp_path)
    (p.models_dir / "vad.onnx").unlink()
    checks = run_checks(p)
    vad = _by_name(checks)["model silero-vad"]
    assert vad.status is Status.FAIL
    assert vad.fix == "run `sombra models download`"
    assert exit_code(checks) == 1


def test_model_checksum_mismatch_fails(tmp_path: Path) -> None:
    p = _probes(tmp_path)
    (p.models_dir / "ggml-w.bin").write_bytes(b"corrupt")
    c = _by_name(run_checks(p))["model whisper w"]
    assert c.status is Status.FAIL
    assert "checksum mismatch" in c.detail
    assert c.fix.startswith("delete ")


def test_old_python_fails(tmp_path: Path) -> None:
    c = _by_name(run_checks(_probes(tmp_path, python_version="3.11.9")))["python"]
    assert c.status is Status.FAIL


@pytest.mark.parametrize(
    ("free", "status"), [(50 * GIB, Status.OK), (3 * GIB, Status.WARN), (GIB // 2, Status.FAIL)]
)
def test_disk_thresholds(tmp_path: Path, free: int, status: Status) -> None:
    seen: list[Path] = []

    def disk_free(path: Path) -> int:
        seen.append(path)
        return free

    c = _by_name(run_checks(_probes(tmp_path, disk_free=disk_free)))["disk"]
    assert c.status is status
    assert seen == [tmp_path]  # nearest existing ancestor of the (missing) meetings root


def test_keychain_unreachable_is_a_warning_everywhere(tmp_path: Path) -> None:
    def broken(provider: str) -> bool:
        raise RuntimeError("no backend")

    checks = run_checks(_probes(tmp_path, has_key=broken))
    names = _by_name(checks)
    assert names["keychain"].status is Status.WARN
    assert "Keychain unreachable" in names["keychain"].detail
    assert names["anthropic API key"].detail == "keychain unreachable"
    assert exit_code(checks) == 0


def test_keychain_unreachable_linux_names_secret_service(tmp_path: Path) -> None:
    def broken(provider: str) -> bool:
        raise RuntimeError("no backend")

    p = _probes(tmp_path, platform="linux", os_name="ubuntu", os_version="24.04", has_key=broken)
    c = _by_name(run_checks(p))["keychain"]
    assert "Secret Service" in c.detail and "gnome-keyring" in c.fix


@pytest.mark.parametrize(
    ("state", "status", "text"),
    [
        (Permission.DENIED, Status.FAIL, "denied for Terminal"),
        (Permission.NOT_DETERMINED, Status.WARN, "not asked yet"),
        (Permission.UNKNOWN, Status.WARN, "not granted or not checkable"),
    ],
)
def test_microphone_permission_states(
    tmp_path: Path, state: Permission, status: Status, text: str
) -> None:
    p = _probes(tmp_path, mic_permission=lambda: state, host_app="Terminal")
    c = _by_name(run_checks(p))["microphone"]
    assert c.status is status
    assert text in c.detail
    assert "System Settings > Privacy & Security > Microphone: enable Terminal" in c.fix


def test_screen_and_accessibility_not_granted_explain_where(tmp_path: Path) -> None:
    p = _probes(
        tmp_path,
        screen_permission=lambda: Permission.UNKNOWN,
        accessibility_permission=lambda: Permission.UNKNOWN,
    )
    names = _by_name(run_checks(p))
    assert "> Screen Recording: enable iTerm" in names["screen recording"].fix
    assert "> Accessibility: enable iTerm" in names["accessibility"].fix


def test_old_macos_fails(tmp_path: Path) -> None:
    c = _by_name(run_checks(_probes(tmp_path, os_version="13.6")))["macOS"]
    assert c.status is Status.FAIL


def test_no_mic_and_no_system_route_warn(tmp_path: Path) -> None:
    p = _probes(tmp_path, default_input=lambda: None, system_audio=lambda: None)
    names = _by_name(run_checks(p))
    assert names["input device"].status is Status.WARN
    assert names["system audio"].status is Status.WARN
    assert "BlackHole" in names["system audio"].fix


def test_loopback_route_ok(tmp_path: Path) -> None:
    c = _by_name(run_checks(_probes(tmp_path, system_audio=lambda: "BlackHole 2ch")))[
        "system audio"
    ]
    assert c.status is Status.OK and "BlackHole 2ch" in c.detail


def test_crashing_probe_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    def boom() -> str | None:
        raise OSError("PortAudio not initialised")

    checks = run_checks(_probes(tmp_path, default_input=boom))
    names = _by_name(checks)
    assert names["input device"] == Check(
        "macos", "input device", Status.WARN, "could not check: OSError: PortAudio not initialised"
    )
    assert names["system audio"].status is Status.OK  # the other checks still ran
    assert exit_code(checks) == 0


def test_ubuntu_live_capture_is_warn_not_fail(tmp_path: Path) -> None:
    p = _probes(tmp_path, platform="linux", os_name="ubuntu", os_version="22.04")
    checks = run_checks(p)
    names = _by_name(checks)
    assert names["distribution"].status is Status.OK
    live = names["live capture"]
    assert live.status is Status.WARN and "ADR 0016" in live.detail
    assert "microphone" not in names  # no macOS checks on Linux
    assert exit_code(checks) == 0


def test_other_linux_distribution_warns(tmp_path: Path) -> None:
    p = _probes(tmp_path, platform="linux", os_name="fedora", os_version="40")
    assert _by_name(run_checks(p))["distribution"].status is Status.WARN


def test_unsupported_platform_fails(tmp_path: Path) -> None:
    checks = run_checks(_probes(tmp_path, platform="win32"))
    assert _by_name(checks)["platform"].status is Status.FAIL


def _agent(p: Probes) -> list[tuple[str, Status]]:
    return [(c.name, c.status) for c in run_checks(p) if c.section == "agent"]


def _agent_checks(p: Probes) -> dict[str, Check]:
    return {c.name: c for c in run_checks(p) if c.section == "agent"}


def test_agent_default_is_the_api_with_a_keychain_key(tmp_path: Path) -> None:
    p = _probes(tmp_path, has_key=lambda provider: provider == "anthropic")
    assert _agent(p) == [
        ("backend", Status.OK),
        ("anthropic API key", Status.OK),
        ("summaries", Status.OK),  # follows the agent: the same key, checked once
    ]


def test_agent_api_backend_without_a_key_fails(tmp_path: Path) -> None:
    checks = run_checks(_probes(tmp_path, has_key=lambda provider: False))
    key = _by_name(checks)["anthropic API key"]
    assert key.status is Status.FAIL and key.fix == "`sombra auth set anthropic`"
    assert exit_code(checks) == 1


def test_agent_claude_code_checks_cli_version_and_login(tmp_path: Path) -> None:
    clis = FakeClis(codex=None)
    p = _probes(
        tmp_path,
        brain=BrainConfig("claude-code"),
        summary_backend="claude-code",
        which=clis.which,
        run=clis.run,
        has_key=lambda provider: False,
        environ={"PATH": "/opt/bin", "HOME": "/home/u", "ANTHROPIC_API_KEY": "sk-shell"},
    )
    assert _agent(p) == [
        ("backend", Status.OK),
        ("claude CLI", Status.OK),
        ("claude login", Status.OK),
        ("summaries", Status.OK),
    ]
    # The status command sees what a meeting will: the CLI's own login, never a key.
    for _argv, env in clis.calls:
        assert "ANTHROPIC_API_KEY" not in env and env["HOME"] == "/home/u"
    assert ["/opt/bin/claude", "auth", "status", "--json"] in [a for a, _ in clis.calls]


@pytest.mark.parametrize(
    ("claude", "name", "status", "fragment"),
    [
        (None, "claude CLI", Status.FAIL, "curl -fsSL https://claude.ai/install.sh | bash"),
        (Cli("2.1.200"), "claude CLI", Status.FAIL, "`claude update`"),
        (Cli("garbled"), "claude CLI", Status.FAIL, "`claude update`"),
        (Cli("2.1.300", "none"), "claude login", Status.FAIL, "claude auth login"),
        (Cli("2.1.285", "api-key"), "claude login", Status.WARN, "log out and back in"),
        (Cli("2.1.285", "garbage"), "claude login", Status.WARN, "claude auth login"),
    ],
)
def test_agent_claude_code_problems(
    tmp_path: Path, claude: Cli | None, name: str, status: Status, fragment: str
) -> None:
    clis = FakeClis(claude=claude, codex=None)
    p = _probes(tmp_path, brain=BrainConfig("claude-code"), which=clis.which, run=clis.run)
    check = _agent_checks(p)[name]
    assert check.status is status and fragment in check.fix


def test_agent_codex_subscription_never_echoes_the_status_output(tmp_path: Path) -> None:
    clis = FakeClis(codex=Cli("0.159.1", "api-key"))
    p = _probes(
        tmp_path,
        brain=BrainConfig("codex", "subscription"),
        summary_backend="codex",
        which=clis.which,
        run=clis.run,
    )
    checks = _agent_checks(p)
    assert checks["codex login"].status is Status.WARN  # billed to the API, not ChatGPT
    assert "openai API key" not in checks  # a subscription never looks at the key
    assert MASKED_KEY not in render_text(run_checks(p))


def test_codex_newer_than_verified_fails_for_the_agent_only(tmp_path: Path) -> None:
    clis = FakeClis(codex=Cli("0.160.0"))
    agent = _agent_checks(
        _probes(
            tmp_path, brain=BrainConfig("codex", "subscription"), which=clis.which, run=clis.run
        )
    )
    assert agent["codex CLI"].status is Status.FAIL
    assert "newer than 0.159.1" in agent["codex CLI"].detail
    assert agent["codex CLI"].fix == "`npm install -g @openai/codex@0.159.1`"
    summaries_only = _agent_checks(
        _probes(
            tmp_path,
            brain=BrainConfig("claude-code"),
            summary_backend="codex",
            which=clis.which,
            run=clis.run,
        )
    )
    assert summaries_only["codex CLI"].status is Status.OK  # the text model has no cap


def test_agent_codex_api_key_summaries_need_the_login(tmp_path: Path) -> None:
    clis = FakeClis(codex=Cli("0.159.1", "none"))
    p = _probes(
        tmp_path,
        brain=BrainConfig("codex", "api-key"),
        summary_backend="codex",
        which=clis.which,
        run=clis.run,
        has_key=lambda provider: provider == "openai",
    )
    assert _agent(p) == [
        ("backend", Status.OK),
        ("codex CLI", Status.OK),
        ("openai API key", Status.OK),
        ("summaries", Status.OK),
        ("codex login", Status.FAIL),  # summaries run on the CLI login only (ADR 0046)
    ]


@pytest.mark.parametrize(
    ("key", "login", "expected"),
    [
        (True, "none", [("openai API key", Status.OK)]),
        (False, "subscription", [("codex login", Status.OK)]),
        (False, "none", [("openai API key", Status.WARN), ("codex login", Status.FAIL)]),
    ],
)
def test_agent_codex_without_auth_takes_a_key_or_the_login(
    tmp_path: Path, key: bool, login: str, expected: list[tuple[str, Status]]
) -> None:
    clis = FakeClis(codex=Cli("0.159.1", login))
    p = _probes(
        tmp_path,
        brain=BrainConfig("codex"),
        summary_backend=None,
        which=clis.which,
        run=clis.run,
        has_key=lambda provider: key,
    )
    assert _agent(p)[2:-1] == expected
    assert _agent(p)[-1] == ("summaries", Status.WARN)  # none: no minutes


def test_agent_keychain_unreachable_is_a_warning(tmp_path: Path) -> None:
    def broken(provider: str) -> bool:
        raise RuntimeError("locked")

    checks = _agent_checks(_probes(tmp_path, has_key=broken))
    assert checks["anthropic API key"].status is Status.WARN


def test_agent_live_answer(tmp_path: Path) -> None:
    ok = _agent_checks(_probes(tmp_path, smoke=lambda: "pronto\n"))
    assert ok["live answer"].status is Status.OK and "pronto" in ok["live answer"].detail

    def fails() -> str:
        raise RuntimeError("Claude Code is not logged in")

    bad = _agent_checks(_probes(tmp_path, smoke=fails))["live answer"]
    assert bad.status is Status.FAIL and "not logged in" in bad.detail


def test_render_text_and_json(tmp_path: Path) -> None:
    p = _probes(tmp_path, mic_permission=lambda: Permission.DENIED)
    checks = run_checks(p)
    text = render_text(checks)
    assert "macOS\n" in text
    assert "  [FAIL] microphone: denied for iTerm" in text
    assert "fix: System Settings > Privacy & Security > Microphone" in text
    assert "note: iTerm is asked for System Audio Recording" in text
    assert text.rstrip().endswith("1 fail")
    data = render_json(checks)
    assert data["ok"] is False
    mic = next(c for c in data["checks"] if c["name"] == "microphone")
    assert mic["status"] == "fail" and mic["section"] == "macos"
    json.dumps(data)  # serialisable


@pytest.mark.parametrize(
    ("env", "app"),
    [
        ({"TERM_PROGRAM": "Apple_Terminal"}, "Terminal"),
        ({"TERM_PROGRAM": "iTerm.app"}, "iTerm"),
        ({"__CFBundleIdentifier": "com.example.Term"}, "com.example.Term"),
        ({}, "the app you run sombra from (Terminal, iTerm, your IDE)"),
    ],
)
def test_host_app(env: dict[str, str], app: str) -> None:
    assert host_app(env) == app


def test_read_os_release() -> None:
    text = 'NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n# comment\n'
    info = read_os_release(text)
    assert info["ID"] == "ubuntu" and info["VERSION_ID"] == "24.04"


def test_whisper_model_name_falls_back_to_default() -> None:
    assert doctor._whisper_model_name("tiny") == "tiny"
    assert doctor._whisper_model_name("large-v3-turbo") == "large-v3-turbo-q5_0"


def test_has_key_uses_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    from sombra.privacy import secrets

    class Backend:
        def get_password(self, service: str, user: str) -> str | None:
            return "k" if user == "anthropic" else None

    monkeypatch.setattr(secrets, "default_backend", Backend)
    assert doctor._has_key("anthropic") is True
    assert doctor._has_key("openai") is False


def test_doctor_command_exit_code_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sombra.cli import main

    probes = _probes(tmp_path, mic_permission=lambda: Permission.DENIED)
    monkeypatch.setattr(doctor, "live_probes", lambda config, models_dir, live: probes)
    assert main(["doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert main(["doctor"]) == 1
    assert "[FAIL] microphone" in capsys.readouterr().out

    ok = _probes(tmp_path)
    monkeypatch.setattr(doctor, "live_probes", lambda config, models_dir, live: ok)
    assert main(["doctor"]) == 0


class _InputOutputPair:
    """Mimics ``sounddevice.default.device``: indexable, but not a tuple."""

    def __init__(self, inp: int | None, out: int | None) -> None:
        self._pair = (inp, out)

    def __getitem__(self, i: int) -> int | None:
        return self._pair[i]


@pytest.mark.parametrize(
    ("device", "index"),
    [
        (_InputOutputPair(3, 5), 3),
        (_InputOutputPair(None, 5), -1),
        (_InputOutputPair(-1, -1), -1),
        ([2, 4], 2),
        (7, 7),
        (None, -1),
    ],
)
def test_input_index(device: object, index: int) -> None:
    assert doctor.input_index(device) == index


def test_missing_whisper_model_fix_names_the_model(tmp_path: Path) -> None:
    p = _probes(tmp_path)
    spec = ModelSpec("whisper tiny", "ggml-tiny.bin", "0" * 64, "sombra models download tiny")
    p = replace(p, models=(spec,))
    c = _by_name(run_checks(p))["model whisper tiny"]
    assert c.fix == "run `sombra models download tiny`"


# --- which app holds the TCC grants (ADR 0050) ---------------------------------------------

SOMBRA_HOLDER = TccHolder("Sombra", doctor.SOMBRA_BUNDLE_ID, "/Applications/Sombra.app")


def test_permission_holder_sombra_app_is_ok(tmp_path: Path) -> None:
    p = _probes(
        tmp_path,
        tcc_holder=SOMBRA_HOLDER,
        running_app="/Applications/Sombra.app",
        app_signature="certificate:Sombra Code Signing",
        host_app="Sombra",
    )
    checks = _by_name(run_checks(p))
    holder = checks["permissions holder"]
    assert holder.status is Status.OK
    assert holder.detail.startswith(f"Sombra ({doctor.SOMBRA_BUNDLE_ID}) at /Applications/")
    assert "Sombra alone" in holder.detail
    assert checks["microphone"].detail == "granted to Sombra"


def test_permission_holder_terminal_for_the_uv_install_suggests_the_app(tmp_path: Path) -> None:
    holder = TccHolder("Terminal", "com.apple.Terminal", "/System/Applications/Terminal.app")
    check = _by_name(run_checks(_probes(tmp_path, tcc_holder=holder)))["permissions holder"]
    assert check.status is Status.OK
    assert check.detail.startswith("Terminal (com.apple.Terminal) at /System/")
    assert "every program" in check.detail
    assert "Sombra.app" in check.fix
    assert "note: install Sombra.app" in render_text([check])


def test_permission_holder_warns_when_the_app_runs_without_the_shim(tmp_path: Path) -> None:
    holder = TccHolder("Terminal", "com.apple.Terminal", "/System/Applications/Terminal.app")
    p = _probes(tmp_path, tcc_holder=holder, running_app="/Applications/Sombra.app")
    check = _by_name(run_checks(p))["permissions holder"]
    assert check.status is Status.WARN
    assert "although sombra runs from /Applications/Sombra.app" in check.detail
    assert "/Applications/Sombra.app/Contents/Helpers/sombra" in check.fix


def test_permission_holder_outside_any_bundle(tmp_path: Path) -> None:
    holder = TccHolder("sshd", "", "/usr/sbin/sshd")
    check = _by_name(run_checks(_probes(tmp_path, tcc_holder=holder)))["permissions holder"]
    assert check.status is Status.OK
    assert check.detail.startswith("sshd at /usr/sbin/sshd:")


def test_permission_holder_unreadable_is_a_warning(tmp_path: Path) -> None:
    p = _probes(tmp_path, tcc_holder=None, host_app="Terminal")
    check = _by_name(run_checks(p))["permissions holder"]
    assert check.status is Status.WARN
    assert "probably Terminal" in check.detail


def test_permission_holder_not_on_linux(tmp_path: Path) -> None:
    p = _probes(tmp_path, platform="linux", os_name="ubuntu", os_version="24.04")
    assert "permissions holder" not in _by_name(run_checks(p))


@pytest.mark.parametrize(
    ("executable", "bundle"),
    [
        ("/Applications/Sombra.app/Contents/MacOS/Sombra", "/Applications/Sombra.app"),
        (
            "/Applications/Visual Studio Code.app/Contents/Frameworks/"
            "Code Helper.app/Contents/MacOS/Code Helper",
            "/Applications/Visual Studio Code.app",
        ),
        ("/usr/sbin/sshd", None),
        ("", None),
    ],
)
def test_app_bundle_of(executable: str, bundle: str | None) -> None:
    assert app_bundle_of(executable) == bundle


def test_holder_from_path_reads_the_bundle_info() -> None:
    infos = {
        Path("/Applications/Sombra.app"): {
            "CFBundleName": "Sombra",
            "CFBundleIdentifier": doctor.SOMBRA_BUNDLE_ID,
        },
        Path("/System/Applications/Utilities/Terminal.app"): {
            "CFBundleDisplayName": "Terminal",
            "CFBundleName": "Terminal-name",
            "CFBundleIdentifier": "com.apple.Terminal",
        },
    }
    sombra = holder_from_path("/Applications/Sombra.app/Contents/MacOS/Sombra", infos.__getitem__)
    assert sombra == SOMBRA_HOLDER
    terminal = holder_from_path(
        "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", infos.__getitem__
    )
    assert terminal == TccHolder(
        "Terminal", "com.apple.Terminal", "/System/Applications/Utilities/Terminal.app"
    )


def test_holder_from_path_without_info_plist_uses_the_bundle_name(tmp_path: Path) -> None:
    exe = tmp_path / "Weird.app" / "Contents" / "MacOS" / "weird"
    assert holder_from_path(str(exe)) == TccHolder("Weird", "", str(tmp_path / "Weird.app"))
    assert holder_from_path("/usr/bin/tmux") == TccHolder("tmux", "", "/usr/bin/tmux")


def test_holder_from_path_reads_a_real_info_plist(tmp_path: Path) -> None:
    bundle = tmp_path / "Sombra.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleName": "Sombra", "CFBundleIdentifier": doctor.SOMBRA_BUNDLE_ID})
    )
    holder = holder_from_path(str(bundle / "Contents" / "MacOS" / "Sombra"))
    assert holder == TccHolder("Sombra", doctor.SOMBRA_BUNDLE_ID, str(bundle))


def test_running_app() -> None:
    exe = "/Applications/Sombra.app/Contents/MacOS/Sombra"
    assert running_app(True, exe) == "/Applications/Sombra.app"
    assert running_app(False, exe) == ""
    assert running_app(True, "/build/dist/Sombra/Sombra") == ""


def test_app_signature_certificate_keeps_the_grants(tmp_path: Path) -> None:
    p = _probes(
        tmp_path,
        tcc_holder=SOMBRA_HOLDER,
        running_app="/Applications/Sombra.app",
        app_signature="certificate:Sombra Code Signing",
    )
    check = _by_name(run_checks(p))["app signature"]
    assert check.status is Status.OK
    assert "'Sombra Code Signing'" in check.detail
    assert "survive upgrades" in check.detail


def test_app_signature_ad_hoc_warns_that_grants_reset(tmp_path: Path) -> None:
    p = _probes(
        tmp_path,
        tcc_holder=SOMBRA_HOLDER,
        running_app="/Applications/Sombra.app",
        app_signature="adhoc",
    )
    check = _by_name(run_checks(p))["app signature"]
    assert check.status is Status.WARN
    assert "forgets the permission grants on every upgrade" in check.detail


def test_app_signature_unknown_warns(tmp_path: Path) -> None:
    p = _probes(tmp_path, running_app="/Applications/Sombra.app", app_signature="")
    check = _by_name(run_checks(p))["app signature"]
    assert check.status is Status.WARN
    assert "codesign -dv /Applications/Sombra.app" in check.fix


def test_app_signature_not_checked_for_the_uv_install(tmp_path: Path) -> None:
    assert "app signature" not in _by_name(run_checks(_probes(tmp_path)))


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        (
            "Identifier=x\nAuthority=Sombra Code Signing\nTeamIdentifier=not set\n",
            "certificate:Sombra Code Signing",
        ),
        ("Identifier=x\nSignature=adhoc\nTeamIdentifier=not set\n", "adhoc"),
        ("/Applications/Sombra.app: code object is not signed at all\n", "unsigned"),
        ("codesign: no such file\n", ""),
    ],
)
def test_parse_signature(output: str, kind: str) -> None:
    assert parse_signature(output) == kind
