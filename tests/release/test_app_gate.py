"""The macOS release gate (``scripts/release/app_gate.py``, ADR 0050).

The rule under test: a final tag never publishes unless Sombra.app is signed by the
project's pinned self-signed certificate (so TCC grants survive upgrades); an ad-hoc
build or any other certificate is refused. A pre-release may ship an ad-hoc app,
labelled as such.
"""

import hashlib
import json
import plistlib
from dataclasses import asdict, replace
from pathlib import Path

import app_gate
import pytest
import sombra_app
from app_gate import ADHOC, CERTIFICATE, AppStatus, decide, main, parse_codesign_display

PINNED = "8a7b2d7884e5aa6f18b6048727483338ff7fbdaf21ed6cca9ff785cdf834dcf2"
OTHER = "0" * 63 + "1"
SHA1 = "0e2de9348dbbbdb43139736307c66406b63ff87d"
PINNED_DR = f'identifier "{sombra_app.BUNDLE_ID}" and certificate root = H"{SHA1}"'

CODESIGN_SELF_SIGNED = f"""\
Executable=/Applications/Sombra.app/Contents/MacOS/Sombra
Identifier={sombra_app.BUNDLE_ID}
Format=app bundle with Mach-O thin (arm64)
CodeDirectory v=20500 size=1234 flags=0x10000(runtime) hashes=28+7 location=embedded
Signature size=1667
Authority=Sombra Code Signing
Signed Time=30 Sep 2026 at 00:10:00
Info.plist entries=20
TeamIdentifier=not set
Runtime Version=14.0.0
"""

CODESIGN_ADHOC = f"""\
Executable=/Applications/Sombra.app/Contents/MacOS/Sombra
Identifier={sombra_app.BUNDLE_ID}
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
        signature=CERTIFICATE,
        authority="Sombra Code Signing",
        identifier=sombra_app.BUNDLE_ID,
        cert_sha256=PINNED,
        designated_requirement=PINNED_DR,
        hardened_runtime=True,
        codesign_strict=True,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _adhoc(zip_path: Path, **kw: object) -> AppStatus:
    dr = 'cdhash H"4b1c0f0e2a9d7c1e3f5a6b7c8d9e0f1a2b3c4d5e"'
    return _status(
        zip_path,
        signature=ADHOC,
        authority="",
        cert_sha256="",
        designated_requirement=dr,
        **kw,
    )


# --- parsing -------------------------------------------------------------------------------


def test_parse_codesign_self_signed() -> None:
    sig = parse_codesign_display(CODESIGN_SELF_SIGNED)
    assert sig.kind == CERTIFICATE
    assert sig.authority == "Sombra Code Signing"
    assert sig.identifier == sombra_app.BUNDLE_ID
    assert sig.hardened_runtime


def test_parse_codesign_adhoc_and_unsigned() -> None:
    sig = parse_codesign_display(CODESIGN_ADHOC)
    assert sig.kind == ADHOC
    assert sig.hardened_runtime
    unsigned = "/x/Sombra.app: code object is not signed at all\n"
    assert parse_codesign_display(unsigned).kind == app_gate.UNSIGNED
    no_runtime = CODESIGN_SELF_SIGNED.replace("flags=0x10000(runtime)", "flags=0x0(none)")
    assert not parse_codesign_display(no_runtime).hardened_runtime


def test_parse_requirement() -> None:
    out = f"Executable=/x/Sombra.app/Contents/MacOS/Sombra\ndesignated => {PINNED_DR}\n"
    assert app_gate.parse_requirement(out) == PINNED_DR
    assert app_gate.parse_requirement("nothing here\n") == ""


@pytest.mark.parametrize(
    ("requirement", "pins"),
    [
        (PINNED_DR, True),
        (f'identifier "{sombra_app.BUNDLE_ID}" and certificate leaf = H"{SHA1}"', True),
        ('cdhash H"4b1c0f0e2a9d7c1e3f5a6b7c8d9e0f1a2b3c4d5e"', False),  # ad-hoc
        (f'identifier "other.app" and certificate leaf = H"{SHA1}"', False),
        (f'identifier "{sombra_app.BUNDLE_ID}"', False),  # no certificate at all
        ("", False),
    ],
)
def test_pins_certificate(requirement: str, pins: bool) -> None:
    assert app_gate.pins_certificate(requirement, sombra_app.BUNDLE_ID) is pins


def test_read_pinned(tmp_path: Path) -> None:
    path = tmp_path / "signing-cert.sha256"
    assert app_gate.read_pinned(path) == ""  # missing file
    path.write_text("# comment only\n\n", encoding="utf-8")
    assert app_gate.read_pinned(path) == ""
    colons = ":".join(PINNED[i : i + 2] for i in range(0, 64, 2)).upper()
    path.write_text(f"# the certificate\n{colons}  # pinned 2026-09-30\n", encoding="utf-8")
    assert app_gate.read_pinned(path) == PINNED
    path.write_text("not-a-fingerprint\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a SHA-256 fingerprint"):
        app_gate.read_pinned(path)


def test_the_committed_pin_file_parses() -> None:
    pinned = app_gate.read_pinned()
    assert pinned == "" or len(pinned) == 64


def test_record_collects_what_the_tools_say(tmp_path: Path) -> None:
    bundle = tmp_path / "Sombra.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(sombra_app.info_plist("0.2.0")))
    zip_path = _zip(tmp_path)
    outputs = {
        "-dv": (0, CODESIGN_SELF_SIGNED),
        "-d -r-": (0, f"designated => {PINNED_DR}\n"),
        "--verify": (0, ""),
    }
    calls = []

    def tool(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        key = "-d -r-" if cmd[1:3] == ["-d", "-r-"] else cmd[1]
        return outputs[key]

    status = app_gate.record(bundle, zip_path, tool, cert_sha256=lambda app: PINNED)
    assert status == _status(zip_path)
    assert ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)] in calls


def test_leaf_certificate_sha256_reads_the_extracted_leaf(tmp_path: Path) -> None:
    def tool(cmd: list[str]) -> tuple[int, str]:
        prefix = cmd[2].removeprefix("--extract-certificates=")
        Path(f"{prefix}0").write_bytes(b"leaf DER")
        Path(f"{prefix}1").write_bytes(b"not used")
        return 0, ""

    expected = hashlib.sha256(b"leaf DER").hexdigest()
    assert app_gate.leaf_certificate_sha256(tmp_path, tool) == expected
    assert app_gate.leaf_certificate_sha256(tmp_path, lambda cmd: (0, "")) == ""  # ad-hoc


# --- decide --------------------------------------------------------------------------------


def test_final_tag_signed_by_the_pinned_certificate_attaches(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path)
    decision = decide(_status(zip_path), "v0.2.0", zip_path, PINNED)
    assert decision.attach
    assert not decision.refuse
    assert decision.app_signature == "pinned"
    assert decision.reasons == []


def test_final_tag_ad_hoc_refuses_to_publish(tmp_path: Path) -> None:
    """The acceptance criterion: no secrets -> ad-hoc app -> a final tag does not publish."""
    zip_path = _zip(tmp_path)
    decision = decide(_adhoc(zip_path), "v0.2.0", zip_path, PINNED)
    assert decision.refuse
    assert not decision.attach
    assert any("signature is adhoc" in r for r in decision.reasons), decision.reasons


def test_final_tag_wrong_certificate_refuses_to_publish(tmp_path: Path) -> None:
    """The other acceptance criterion: a certificate that isn't the pinned one."""
    zip_path = _zip(tmp_path)
    wrong = _status(zip_path, cert_sha256=OTHER, authority="Someone Else")
    decision = decide(wrong, "v0.2.0", zip_path, PINNED)
    assert decision.refuse
    assert any(f"signed by certificate {OTHER} (Someone Else)" in r for r in decision.reasons)


def test_final_tag_without_a_pinned_certificate_refuses(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path)
    decision = decide(_status(zip_path), "v0.2.0", zip_path, pinned="")
    assert decision.refuse
    assert any("no certificate is pinned" in r for r in decision.reasons)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"designated_requirement": 'cdhash H"00"'}, "does not pin the identifier"),
        ({"hardened_runtime": False}, "hardened runtime is off"),
        ({"codesign_strict": False}, "--deep --strict failed"),
        ({"signature": "unsigned"}, "signature is unsigned"),
    ],
)
def test_final_tag_refuses_any_missing_piece(
    tmp_path: Path, change: dict[str, object], reason: str
) -> None:
    zip_path = _zip(tmp_path)
    decision = decide(_status(zip_path, **change), "v0.2.0", zip_path, PINNED)
    assert decision.refuse
    assert any(reason in r for r in decision.reasons), decision.reasons


def test_prerelease_ad_hoc_publishes_labelled(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path, "0.2.0rc1")
    decision = decide(_adhoc(zip_path, version="0.2.0rc1"), "v0.2.0-rc1", zip_path, PINNED)
    assert not decision.refuse
    assert decision.attach
    assert decision.app_signature == "adhoc"
    assert decision.reasons  # said as warnings


def test_prerelease_signed_by_the_pinned_certificate(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path, "0.2.0rc1")
    decision = decide(_status(zip_path, version="0.2.0rc1"), "v0.2.0-rc1", zip_path, PINNED)
    assert decision.attach
    assert decision.app_signature == "pinned"


def test_prerelease_wrong_certificate_or_broken_ad_hoc_refuses(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path, "0.2.0rc1")
    wrong = _status(zip_path, version="0.2.0rc1", cert_sha256=OTHER)
    assert decide(wrong, "v0.2.0-rc1", zip_path, PINNED).refuse
    broken = _adhoc(zip_path, version="0.2.0rc1", codesign_strict=False)
    assert decide(broken, "v0.2.0-rc1", zip_path, PINNED).refuse


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
        decision = decide(bad, tag, zip_path, PINNED)
        assert decision.refuse
        assert any(reason in r for r in decision.reasons), decision.reasons


def test_wrong_asset_name_or_missing_zip_refuses(tmp_path: Path) -> None:
    other = tmp_path / "Sombra.zip"
    other.write_bytes(b"x")
    decision = decide(_status(other), "v0.2.0", other, PINNED)
    assert decision.refuse
    assert any("expected Sombra-0.2.0-macos-arm64.zip" in r for r in decision.reasons)
    missing = tmp_path / sombra_app.zip_name("0.2.0")
    decision = decide(_status(other), "v0.2.0", missing, PINNED)
    assert decision.refuse
    assert any("not found" in r for r in decision.reasons)


def test_bad_tag_refuses(tmp_path: Path) -> None:
    zip_path = _zip(tmp_path)
    assert decide(_status(zip_path), "latest", zip_path, PINNED).refuse


# --- CLI -----------------------------------------------------------------------------------


def _write_status(tmp_path: Path, status: AppStatus) -> Path:
    path = tmp_path / "app-status.json"
    path.write_text(json.dumps(asdict(status)), encoding="utf-8")
    return path


def _decide_argv(status: Path, tag: str, zip_path: Path) -> list[str]:
    return ["decide", "--status", str(status), "--tag", tag, "--zip", str(zip_path)]


def test_cli_final_tag_ad_hoc_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_path = _zip(tmp_path)
    status = _write_status(tmp_path, _adhoc(zip_path))
    out = tmp_path / "github-output"
    argv = [*_decide_argv(status, "v0.2.0", zip_path), "--pinned-sha256", PINNED]
    assert main([*argv, "--github-output", str(out)]) == 1
    err = capsys.readouterr().err
    assert "::error::app gate (v0.2.0): signature is adhoc" in err
    assert "refusing to publish v0.2.0" in err
    assert not out.exists()


def test_cli_pinned_final_tag_prints_attach(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zip_path = _zip(tmp_path)
    status = _write_status(tmp_path, _status(zip_path))
    out = tmp_path / "github-output"
    colons = ":".join(PINNED[i : i + 2] for i in range(0, 64, 2)).upper()
    argv = [*_decide_argv(status, "v0.2.0", zip_path), "--pinned-sha256", colons]
    assert main([*argv, "--github-output", str(out)]) == 0
    assert capsys.readouterr().out == "attach=true\napp_signature=pinned\n"
    assert out.read_text(encoding="utf-8") == "attach=true\napp_signature=pinned\n"


def test_cli_prerelease_ad_hoc_warns_and_labels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zip_path = _zip(tmp_path, "0.2.0rc1")
    status = _write_status(tmp_path, _adhoc(zip_path, version="0.2.0rc1"))
    assert main(_decide_argv(status, "v0.2.0-rc1", zip_path)) == 0
    captured = capsys.readouterr()
    assert captured.out == "attach=true\napp_signature=adhoc\n"
    assert "::warning::app gate (v0.2.0-rc1)" in captured.err


def test_cli_uses_the_committed_pin_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pin = tmp_path / "signing-cert.sha256"
    pin.write_text(PINNED + "\n", encoding="utf-8")
    monkeypatch.setattr(app_gate, "PINNED_FILE", pin)
    zip_path = _zip(tmp_path)
    status = _write_status(tmp_path, _status(zip_path))
    assert main(_decide_argv(status, "v0.2.0", zip_path)) == 0
    assert "app_signature=pinned" in capsys.readouterr().out


def test_cli_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    zip_path = _zip(tmp_path)
    adhoc = _write_status(tmp_path, _adhoc(zip_path))
    assert main(["ready", "--status", str(adhoc), "--pinned-sha256", PINNED]) == 1
    assert "not release-ready: signature is adhoc" in capsys.readouterr().out
    signed = _write_status(tmp_path, _status(zip_path))
    assert main(["ready", "--status", str(signed), "--pinned-sha256", PINNED]) == 0
    assert capsys.readouterr().out.startswith("release-ready: signed by the pinned certificate")
