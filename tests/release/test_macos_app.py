"""Sombra.app's definition, build helpers and signing order (``packaging/macos``, ADR 0050).

The bundle itself is built, signed and smoke-tested on macOS by the release workflow;
these tests cover the logic that decides what goes into it.
"""

import plistlib
import re
from pathlib import Path

import build
import pytest
import sign
import sombra_app

from sombra.config import doctor

ROOT = Path(__file__).resolve().parents[2]
MACHO_64 = bytes.fromhex("cffaedfe") + b"\0" * 60
FAT = bytes.fromhex("cafebabe") + b"\0" * 60


def test_bundle_id_is_the_same_everywhere() -> None:
    assert doctor.SOMBRA_BUNDLE_ID == sombra_app.BUNDLE_ID
    assert doctor.SHIM_RELPATH == sombra_app.SHIM_RELPATH
    install_sh = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert f'BUNDLE_ID="{sombra_app.BUNDLE_ID}"' in install_sh
    assert f'SHIM_RELPATH="{sombra_app.SHIM_RELPATH}"' in install_sh


def test_zip_name_is_the_asset_the_cask_expects() -> None:
    assert sombra_app.zip_name("0.2.0") == "Sombra-0.2.0-macos-arm64.zip"
    assert sombra_app.zip_name("0.2.0rc1") == "Sombra-0.2.0rc1-macos-arm64.zip"
    assert sombra_app.zip_version("Sombra-0.2.0-macos-arm64.zip") == "0.2.0"
    assert sombra_app.zip_version("Sombra-0.2.1.dev4+g1a2b3c4-macos-arm64.zip") == (
        "0.2.1.dev4+g1a2b3c4"
    )
    assert sombra_app.zip_version("sombra-0.2.0-py3-none-any.whl") is None
    for bad in ("", "0.2/0", "0.2.0-rc1"):
        with pytest.raises(ValueError, match="not a release version"):
            sombra_app.zip_name(bad)


def test_info_plist() -> None:
    info = sombra_app.info_plist("0.2.0")
    assert info["CFBundleIdentifier"] == sombra_app.BUNDLE_ID
    assert info["CFBundleShortVersionString"] == "0.2.0"
    assert info["CFBundleVersion"] == "0.2.0"
    assert info["CFBundleExecutable"] == sombra_app.EXECUTABLE
    assert info["LSMinimumSystemVersion"] == "14.0"
    assert info["NSMicrophoneUsageDescription"].startswith("Sombra transcribes your voice")
    assert set(info["CFBundleLocalizations"]) == {"en", "pt-BR"}
    plistlib.dumps(info)  # serialisable as a real Info.plist


def test_usage_strings_exist_in_both_languages() -> None:
    en, pt = sombra_app.USAGE["en"], sombra_app.USAGE["pt-BR"]
    assert set(en) == set(pt)
    assert {"NSMicrophoneUsageDescription", "NSAudioCaptureUsageDescription"} <= set(en)
    assert all(text.strip() and text != en[key] for key, text in pt.items())
    assert "reuniões" in pt["NSMicrophoneUsageDescription"]


def test_infoplist_strings_format() -> None:
    text = sombra_app.infoplist_strings("pt-BR")
    entries = re.findall(r'^"(\w+)" = "((?:[^"\\]|\\.)*)";$', text, flags=re.M)
    assert dict(entries) == sombra_app.USAGE["pt-BR"]
    tricky = sombra_app._strings_escape('say "hi"\\\n')
    assert tricky == 'say \\"hi\\"\\\\\\n'


def test_entitlements_are_only_what_the_adr_justifies() -> None:
    path = ROOT / "packaging" / "macos" / "entitlements.plist"
    entitlements = plistlib.loads(path.read_bytes())
    assert entitlements == {"com.apple.security.device.audio-input": True}
    adr = next((ROOT / "docs" / "adr").glob("0050-*.md")).read_text(encoding="utf-8")
    for key in entitlements:
        assert key in adr


def test_wheel_version() -> None:
    assert build.wheel_version(Path("dist/sombra-0.2.0rc1-py3-none-any.whl")) == "0.2.0rc1"
    for bad in ("sombra-0.2.0.tar.gz", "other-0.2.0-py3-none-any.whl"):
        with pytest.raises(ValueError, match="not a sombra wheel"):
            build.wheel_version(Path(bad))


def test_write_localizations(tmp_path: Path) -> None:
    bundle = tmp_path / "Sombra.app"
    written = build.write_localizations(bundle)
    names = sorted(str(p.relative_to(bundle)) for p in written)
    assert names == [
        "Contents/Resources/en.lproj/InfoPlist.strings",
        "Contents/Resources/pt-BR.lproj/InfoPlist.strings",
    ]
    assert "reunião" in (bundle / names[1]).read_text(encoding="utf-8")


def test_shim_command() -> None:
    out = Path("Sombra.app/Contents/Helpers/sombra")
    mac = build.shim_command("clang", out, darwin=True)
    assert mac[:1] == ["clang"]
    assert mac[mac.index("-arch") : mac.index("-arch") + 2] == ["-arch", "arm64"]
    assert "-mmacosx-version-min=14.0" in mac
    assert mac[-1].endswith("shim/sombra.c")
    assert "-ldl" in build.shim_command("cc", out, darwin=False)


def test_build_runs_the_locked_steps_in_order(tmp_path: Path) -> None:
    wheel = tmp_path / "sombra-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"")
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        calls.append((list(cmd), env))

    out = build.build(wheel, tmp_path / "out", run=run)
    steps = [" ".join(c[1:3]) for c, _ in calls]
    assert steps[:4] == ["export --frozen", "venv --python", "pip install", "pip install"]
    assert "--group app" in " ".join(calls[0][0])
    assert "--no-deps" in calls[3][0]
    assert calls[4][0][0].endswith("pyinstaller")
    assert all(env and env["SOMBRA_VERSION"] == "0.2.0" for _, env in calls[:5])
    assert all(env and env["UV_PYTHON_PREFERENCE"] == "only-managed" for _, env in calls[:5])
    if out.suffix == ".app":  # macOS: strings written, shim compiled into the bundle
        assert calls[-1][0][-1].endswith("shim/sombra.c")
        assert (out / "Contents/Resources/pt-BR.lproj/InfoPlist.strings").is_file()


def test_tree_size_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "link").symlink_to(tmp_path / "a")
    assert build.tree_size(tmp_path) == 5


# --- signing ---------------------------------------------------------------------------------


def _fake_app(tmp_path: Path) -> Path:
    bundle = tmp_path / "Sombra.app"
    contents = bundle / "Contents"
    for rel, data in {
        "Info.plist": plistlib.dumps(sombra_app.info_plist("0.2.0")),
        "MacOS/Sombra": MACHO_64,
        "Helpers/sombra": MACHO_64,
        "Frameworks/libpython3.12.dylib": MACHO_64,
        "Frameworks/numpy/_core/_multiarray_umath.cpython-312-darwin.so": FAT,
        "Frameworks/onnxruntime/capi/libonnxruntime.1.30.0.dylib": MACHO_64,
        "Frameworks/Nested.framework/Versions/A/Nested": MACHO_64,
        "Resources/base_library.zip": b"PK\x03\x04",
        "Resources/sombra/brain/system_prompt_pt.md": b"# prompt",
    }.items():
        path = contents / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (contents / "Frameworks" / "libpython-link.dylib").symlink_to("libpython3.12.dylib")
    return bundle


def test_is_macho(tmp_path: Path) -> None:
    bundle = _fake_app(tmp_path)
    assert sign.is_macho(bundle / "Contents/MacOS/Sombra")
    assert not sign.is_macho(bundle / "Contents/Resources/base_library.zip")
    assert not sign.is_macho(bundle / "Contents/Frameworks/libpython-link.dylib")  # symlink
    assert not sign.is_macho(bundle / "Contents/Frameworks")


def test_signing_plan_is_inside_out(tmp_path: Path) -> None:
    bundle = _fake_app(tmp_path)
    files, nested = sign.signing_plan(bundle)
    rel = [str(p.relative_to(bundle)) for p in files]
    assert "Contents/MacOS/Sombra" not in rel  # signed with the bundle
    assert "Contents/Helpers/sombra" not in rel  # signed with its own identifier
    assert "Contents/Frameworks/libpython-link.dylib" not in rel
    assert rel[0] == "Contents/Frameworks/Nested.framework/Versions/A/Nested"  # deepest
    assert set(rel) == {
        "Contents/Frameworks/numpy/_core/_multiarray_umath.cpython-312-darwin.so",
        "Contents/Frameworks/onnxruntime/capi/libonnxruntime.1.30.0.dylib",
        "Contents/Frameworks/Nested.framework/Versions/A/Nested",
        "Contents/Frameworks/libpython3.12.dylib",
    }
    depths = [len(p.parts) for p in files]
    assert depths == sorted(depths, reverse=True)
    assert [p.name for p in nested] == ["Nested.framework"]


def test_commands(tmp_path: Path) -> None:
    bundle = _fake_app(tmp_path)
    ent = tmp_path / "entitlements.plist"
    cmds = sign.commands(bundle, "0E2DE934", ent)
    for cmd in cmds[:-1]:
        assert cmd[:7] == [
            *("codesign", "--force", "--sign", "0E2DE934"),
            *("--options", "runtime", "--timestamp=none"),
        ]
    shim, app_cmd, verify = cmds[-3], cmds[-2], cmds[-1]
    assert shim[-3:] == ["--identifier", sombra_app.SHIM_ID, str(bundle / sombra_app.SHIM_RELPATH)]
    assert app_cmd[-3:] == ["--entitlements", str(ent), str(bundle)]
    assert verify == ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)]
    # Everything nested comes before the bundle that seals it.
    order = [c[-1] for c in cmds[:-1]]
    assert order.index(str(bundle / "Contents/Frameworks/Nested.framework")) < order.index(
        str(bundle)
    )


def test_commands_are_batched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _fake_app(tmp_path)
    monkeypatch.setattr(sign, "BATCH", 2)
    cmds = sign.commands(bundle, "-", tmp_path / "e.plist")
    file_calls = [c for c in cmds if c[-1].endswith((".so", ".dylib", "Nested"))]
    assert [len(c) - 7 for c in file_calls] == [2, 2]  # 4 files in batches of 2


def test_sign_runs_everything_and_rejects_non_bundles(tmp_path: Path) -> None:
    bundle = _fake_app(tmp_path)
    ran: list[list[str]] = []
    count = sign.sign(bundle, "-", tmp_path / "e.plist", run=lambda c: ran.append(list(c)))
    assert count == len(ran) and ran[-1][1] == "--verify"
    with pytest.raises(FileNotFoundError, match="not an app bundle"):
        sign.sign(tmp_path, "-", tmp_path / "e.plist", run=lambda c: None)
