"""``sombra doctor`` checks against fake probes (no OS calls)."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from sombra.config import doctor
from sombra.config.doctor import (
    Check,
    ModelSpec,
    Permission,
    Probes,
    Status,
    exit_code,
    host_app,
    read_os_release,
    render_json,
    render_text,
    run_checks,
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


def test_agent_section_reports_clis_and_keys(tmp_path: Path) -> None:
    p = _probes(
        tmp_path,
        which=lambda name: "/opt/bin/claude" if name == "claude" else None,
        has_key=lambda provider: provider == "openai",
    )
    agent = [c for c in run_checks(p) if c.section == "agent"]
    assert [(c.name, c.status) for c in agent] == [
        ("claude CLI", Status.OK),
        ("codex CLI", Status.WARN),
        ("anthropic API key", Status.WARN),
        ("openai API key", Status.OK),
    ]
    assert agent[1].fix == "install the Codex CLI (`npm i -g @openai/codex`) and run `codex login`"
    assert agent[2].fix == "`sombra auth set anthropic`"


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
    monkeypatch.setattr(doctor, "live_probes", lambda config, models_dir: probes)
    assert main(["doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert main(["doctor"]) == 1
    assert "[FAIL] microphone" in capsys.readouterr().out

    ok = _probes(tmp_path)
    monkeypatch.setattr(doctor, "live_probes", lambda config, models_dir: ok)
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
