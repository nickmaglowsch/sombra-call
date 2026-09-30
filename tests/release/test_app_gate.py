"""The macOS release gate (``scripts/release/app_gate.py``, ADR 0050).

The rule under test: a final tag never publishes unless Sombra.app is Developer ID
signed, notarized and stapled; a pre-release publishes without the app instead.
"""

import json
import plistlib
from dataclasses import asdict, replace
from pathlib import Path

import app_gate
import pytest
import sombra_app
from app_gate import ADHOC, DEVELOPER_ID, AppStatus, decide, main, parse_codesign_display

CODESIGN_DEVELOPER_ID = """\
Executable=/Applications/Sombra.app/Contents/MacOS/Sombra
Identifier=io.github.nickmaglowsch.Sombra
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20500 size=1234 flags=0x10000(runtime) hashes=28+7 location=embedded
Signature size=9045
Authority=Developer ID Application: Example Person (ABCDE12345)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
Timestamp=29 Sep 2026 at 12:00:00
Info.plist entries=20
TeamIdentifier=ABCDE12345
Runtime Version=14.0.0
"""

CODESIGN_ADHOC = """\
Executable=/Applications/Sombra.app/Contents/MacOS/Sombra
Identifier=io.github.nickmaglowsch.Sombra
CodeDirectory v=20500 size=1234 flags=0x10002(adhoc,runtime) hashes=28+7 location=embedded
Signature=adhoc
Info.plist entries=20
TeamIdentifier=not set
"""


def _zip(tmp_path: Path, version: str = "0.2.0") -> Path:
    path = tmp_path / sombra_app.zip_name(version)
    path.write_bytes(b"PK zip of Sombra.app")
    return path


def _status(zip_path: Path, **kw: object) -> AppStatus:
    base = AppStatus(
        zip=zip_path.name,
        sha256=app_gate.sha256_file(zip_path),
        bundle_id=sombra_app.BUNDLE_ID,
        version="0.2.0",
        signature=DEVELOPER_ID,
        team_id="ABCDE12345",
        identifier=sombra_app.BUNDLE_ID,
        hardened_runtime=True,
        codesign_strict=True,
        spctl=True,
        spctl_source="Notarized Developer ID",
        stapled=True,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _adhoc(zip_path: Path, **kw: object) -> AppStatus:
    return _status(
        zip_path,
        signature=ADHOC,
        team_id="",
        spctl=False,
        spctl_source="",
        stapled=False,
        **kw,
    )


# --- parsing -------------------------------------------------------------------------------


def test_parse_codesign_developer_id() -> None:
    sig = parse_codesign_display(CODESIGN_DEVELOPER_ID)
    assert sig.kind == DEVELOPER_ID
    assert sig.team_id == "ABCDE12345"
    assert sig.identifier == sombra_app.BUNDLE_ID
    assert sig.hardened_runtime


def test_parse_codesign_adhoc() -> None:
    sig = parse_codesign_display(CODESIGN_ADHOC)
    assert sig.kind == ADHOC
    assert sig.team_id == ""
    assert sig.hardened_runtime


def test_parse_codesign_unsigned_and_other() -> None:
    unsigned = "/x/Sombra.app: code object is not signed at all\n"
    assert parse_codesign_display(unsigned).kind == app_gate.UNSIGNED
    dev = CODESIGN_DEVELOPER_ID.replace("Developer ID Application", "Apple Development")
    dev = dev.replace("flags=0x10000(runtime)", "flags=0x0(none)")
    sig = parse_codesign_display(dev)
    assert sig.kind == app_gate.OTHER
    assert not sig.hardened_runtime


def test_parse_spctl_source() -> None:
    accepted = "/Applications/Sombra.app: accepted\nsource=Notarized Developer ID\norigin=X\n"
    assert app_gate.parse_spctl_source(accepted) == "Notarized Developer ID"
    assert app_gate.parse_spctl_source("/x: rejected\n") == ""


def test_record_collects_what_the_tools_say(tmp_path: Path) -> None:
    bundle = tmp_path / "Sombra.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(sombra_app.info_plist("0.2.0")))
    zip_path = _zip(tmp_path)
    outputs = {
        "codesign -dv": (0, CODESIGN_DEVELOPER_ID),
        "codesign --verify": (0, ""),
        "spctl --assess": (0, "accepted\nsource=Notarized Developer ID\n"),
        "xcrun stapler": (0, "The validate action worked!\n"),
    }
    calls = []

    def tool(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        return outputs[" ".join(cmd[:2])]

    status = app_gate.record(bundle, zip_path, tool)
    assert status == _status(zip_path)
    assert ["spctl", "--assess", "--type", "execute", "-vv", str(bundle)] in calls
    assert ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)] in calls


# --- decide --------------------------------------------------------------------------------


def test_final_tag_with_a_notarized_app_attaches_it(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path)
    decision = decide(_status(zip_path), "v0.2.0", zip_path, team_id="ABCDE12345")
    assert decision.attach
    assert not decision.refuse
    assert decision.reasons == []


def test_final_tag_without_notarization_refuses_to_publish(tmp_path: Path) -> None:
    """The acceptance criterion: no secrets -> ad-hoc app -> a final tag does not publish."""
    zip_path = _zip(tmp_path)
    decision = decide(_adhoc(zip_path), "v0.2.0", zip_path)
    assert decision.refuse
    assert not decision.attach
    text = "\n".join(decision.reasons)
    assert "adhoc, not Developer ID" in text
    assert "not notarized" in text
    assert "no stapled" in text


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"spctl": False, "spctl_source": ""}, "not notarized"),
        ({"spctl_source": "Developer ID"}, "not notarized"),  # signed, never notarized
        ({"stapled": False}, "no stapled notarization ticket"),
        ({"hardened_runtime": False}, "hardened runtime is off"),
        ({"codesign_strict": False}, "--deep --strict failed"),
        ({"signature": "other"}, "signature is other"),
        ({"team_id": "ZZZZZ99999"}, "expected ABCDE12345"),
    ],
)
def test_final_tag_refuses_any_missing_piece(
    tmp_path: Path, change: dict[str, object], reason: str
) -> None:
    zip_path = _zip(tmp_path)
    decision = decide(_status(zip_path, **change), "v0.2.0", zip_path, team_id="ABCDE12345")
    assert decision.refuse
    assert any(reason in r for r in decision.reasons), decision.reasons


def test_prerelease_without_notarization_publishes_without_the_app(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path, "0.2.0rc1")
    decision = decide(_adhoc(zip_path, version="0.2.0rc1"), "v0.2.0-rc1", zip_path)
    assert not decision.refuse
    assert not decision.attach
    assert decision.reasons  # said as warnings


def test_prerelease_with_notarization_attaches(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path, "0.2.0rc1")
    decision = decide(_status(zip_path, version="0.2.0rc1"), "v0.2.0-rc1", zip_path)
    assert decision.attach


@pytest.mark.parametrize("tag", ["v0.2.0", "v0.2.0-rc1"])
def test_integrity_problems_refuse_every_tag(tmp_path: Path, tag: str) -> None:
    version = "0.2.0" if tag == "v0.2.0" else "0.2.0rc1"
    zip_path = _zip(tmp_path, version)
    good = _status(zip_path, version=version)
    for bad, reason in [
        (replace(good, sha256="0" * 64), "sha256 differs"),
        (replace(good, version="0.1.9"), "CFBundleShortVersionString is 0.1.9"),
        (replace(good, bundle_id="com.example.other"), "bundle id is com.example.other"),
    ]:
        decision = decide(bad, tag, zip_path)
        assert decision.refuse
        assert any(reason in r for r in decision.reasons), decision.reasons


def test_wrong_asset_name_or_missing_zip_refuses(tmp_path: Path) -> None:
    other = tmp_path / "Sombra.zip"
    other.write_bytes(b"x")
    decision = decide(_status(other), "v0.2.0", other)
    assert decision.refuse
    assert any("expected Sombra-0.2.0-macos-arm64.zip" in r for r in decision.reasons)
    missing = tmp_path / sombra_app.zip_name("0.2.0")
    decision = decide(_status(other), "v0.2.0", missing)
    assert decision.refuse
    assert any("not found" in r for r in decision.reasons)


def test_bad_tag_refuses(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path)
    decision = decide(_status(zip_path), "latest", zip_path)
    assert decision.refuse


# --- CLI -----------------------------------------------------------------------------------


def _write_status(tmp_path: Path, status: AppStatus) -> Path:
    path = tmp_path / "app-status.json"
    path.write_text(json.dumps(asdict(status)), encoding="utf-8")
    return path


def test_cli_final_tag_unsigned_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_path = _zip(tmp_path)
    status = _write_status(tmp_path, _adhoc(zip_path))
    out = tmp_path / "github-output"
    argv = ["decide", "--status", str(status), "--tag", "v0.2.0", "--zip", str(zip_path)]
    assert main([*argv, "--github-output", str(out)]) == 1
    err = capsys.readouterr().err
    assert "::error::app gate (v0.2.0): not notarized" in err
    assert "refusing to publish v0.2.0" in err
    assert not out.exists()


def test_cli_signed_final_tag_prints_attach(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zip_path = _zip(tmp_path)
    status = _write_status(tmp_path, _status(zip_path))
    out = tmp_path / "github-output"
    argv = ["decide", "--status", str(status), "--tag", "v0.2.0", "--zip", str(zip_path)]
    assert main([*argv, "--team-id", "ABCDE12345", "--github-output", str(out)]) == 0
    assert capsys.readouterr().out == "attach=true\n"
    assert out.read_text(encoding="utf-8") == "attach=true\n"


def test_cli_prerelease_unsigned_warns_and_skips_the_app(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zip_path = _zip(tmp_path, "0.2.0rc1")
    status = _write_status(tmp_path, _adhoc(zip_path, version="0.2.0rc1"))
    argv = ["decide", "--status", str(status), "--tag", "v0.2.0-rc1", "--zip", str(zip_path)]
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "attach=false\n"
    assert "::warning::app gate (v0.2.0-rc1)" in captured.err


def test_cli_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_path = _zip(tmp_path)
    adhoc = _write_status(tmp_path, _adhoc(zip_path))
    assert main(["ready", "--status", str(adhoc)]) == 1
    assert "not release-ready: not notarized" in capsys.readouterr().out
    signed = _write_status(tmp_path, _status(zip_path))
    assert main(["ready", "--status", str(signed), "--team-id", "ABCDE12345"]) == 0
    assert capsys.readouterr().out.startswith("release-ready:")
