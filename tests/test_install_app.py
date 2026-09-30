"""``scripts/install.sh`` installing ``Sombra.app`` on macOS (ADR 0050), with fake tools.

``uname``, ``sw_vers``, ``ditto``, ``plutil``, ``codesign`` and ``spctl`` are fakes on
PATH, so the macOS flow runs on any CI runner. The real thing (a real zip, real
codesign and Gatekeeper) runs in the release workflow's app smoke job.
"""

import hashlib
import plistlib
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"
sys.path.insert(0, str(ROOT / "packaging" / "macos"))

import sombra_app  # noqa: E402  # packaging/macos, added to sys.path above

PY = sys.executable

FAKES = {
    "uname": '#!/bin/sh\ncase "$1" in -m) echo arm64 ;; *) echo Darwin ;; esac\n',
    "sw_vers": "#!/bin/sh\necho 14.5\n",
    "ditto": f"""#!/bin/sh
if [ "$1" = "-x" ]; then
    exec "{PY}" -c '
import os, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    for info in z.infolist():
        path = z.extract(info, sys.argv[2])
        mode = info.external_attr >> 16
        if mode:
            os.chmod(path, mode)
' "$3" "$4"
fi
exec cp -R "$1" "$2"
""",
    "plutil": f"""#!/bin/sh
exec "{PY}" -c '
import plistlib, sys
print(plistlib.load(open(sys.argv[1], "rb"))[sys.argv[2]])
' "$6" "$2"
""",
    "codesign": '#!/bin/sh\nexit "${FAKE_CODESIGN_RC:-0}"\n',
    "spctl": "#!/bin/sh\nexit 3\n",  # not notarized, as on a PR build
    # uv: records what it is asked; `tool list` lists sombra when FAKE_UV_HAS_SOMBRA=1.
    "uv": """#!/bin/sh
echo "uv $*" >> "$HOME/uv.log"
case "$1 $2" in
  "tool dir") echo "$HOME/.local/bin" ;;
  "tool list") [ "${FAKE_UV_HAS_SOMBRA:-0}" = 1 ] && echo "sombra v0.1.0" ;;
  "tool install")
      mkdir -p "$HOME/.local/bin"
      printf '#!/bin/sh\\necho sombra 0.1.0-uv\\n' > "$HOME/.local/bin/sombra"
      chmod +x "$HOME/.local/bin/sombra" ;;
  "--version ") echo "uv 0.8.0" ;;
esac
exit 0
""",
}

# The app's CLI shim, faked as a script that behaves like `sombra`.
FAKE_SHIM = """#!/bin/sh
case "$1" in
  --version) echo "sombra 0.2.0" ;;
  models) echo "$HOME/.cache/sombra/models" ;;
  *) echo "sombra $*" ;;
esac
"""


def _make_zip(path: Path, bundle_id: str = sombra_app.BUNDLE_ID) -> Path:
    info = {**sombra_app.info_plist("0.2.0"), "CFBundleIdentifier": bundle_id}
    executable = stat.S_IFREG | 0o755
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Sombra.app/Contents/Info.plist", plistlib.dumps(info))
        for rel, text in {
            "Sombra.app/Contents/MacOS/Sombra": "#!/bin/sh\n",
            f"Sombra.app/{sombra_app.SHIM_RELPATH}": FAKE_SHIM,
        }.items():
            entry = zipfile.ZipInfo(rel)
            entry.external_attr = executable << 16
            z.writestr(entry, text)
    return path


@pytest.fixture
def mac(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name, text in FAKES.items():
        (fakebin / name).write_text(text, encoding="utf-8")
        (fakebin / name).chmod(0o755)
    apps = tmp_path / "Applications"
    return {"home": home, "fakebin": fakebin, "apps": apps, "tmp": tmp_path}


def _install(mac: dict[str, Path], *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed argv, no shell
        ["sh", str(SCRIPT), *args],  # noqa: S607  # POSIX sh
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={
            "HOME": str(mac["home"]),
            "PATH": f"{mac['fakebin']}:/usr/bin:/bin",
            "SOMBRA_APP_DIR": str(mac["apps"]),
            **env,
        },
    )


def _link(mac: dict[str, Path]) -> Path:
    return mac["home"] / ".local" / "bin" / "sombra"


def test_installs_the_app_and_links_the_shim(mac: dict[str, Path]) -> None:
    zip_path = _make_zip(mac["tmp"] / "Sombra-0.2.0-macos-arm64.zip")
    result = _install(mac, "--from-app-zip", str(zip_path), "--yes", "--no-models")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    app = mac["apps"] / "Sombra.app"
    assert (app / "Contents" / "Info.plist").is_file()
    link = _link(mac)
    assert link.is_symlink()
    assert link.resolve() == (app / sombra_app.SHIM_RELPATH).resolve()
    assert "sombra 0.2.0" in result.stdout  # ran through the link
    assert "Gatekeeper does not accept this Sombra.app" in result.stderr
    assert "to Sombra (System Settings" in result.stdout  # the next steps name Sombra
    assert not (mac["home"] / "uv.log").exists() or "tool install" not in (
        mac["home"] / "uv.log"
    ).read_text(encoding="utf-8")


def test_rerun_upgrades_in_place_and_replaces_a_uv_install(mac: dict[str, Path]) -> None:
    zip_path = _make_zip(mac["tmp"] / "Sombra-0.2.0-macos-arm64.zip")
    assert _install(mac, "--from-app-zip", str(zip_path), "--yes", "--no-models").returncode == 0
    marker = mac["apps"] / "Sombra.app" / "Contents" / "old-file"
    marker.write_text("from the previous version")
    result = _install(
        mac, "--from-app-zip", str(zip_path), "--yes", "--no-models", FAKE_UV_HAS_SOMBRA="1"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "upgrading" in result.stdout
    assert not marker.exists()  # the old bundle was replaced, not merged
    assert "uv tool uninstall sombra" in (mac["home"] / "uv.log").read_text(encoding="utf-8")


def test_refuses_to_replace_another_app(mac: dict[str, Path]) -> None:
    other = mac["apps"] / "Sombra.app" / "Contents"
    other.mkdir(parents=True)
    (other / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.other.App"}))
    zip_path = _make_zip(mac["tmp"] / "Sombra-0.2.0-macos-arm64.zip")
    result = _install(mac, "--from-app-zip", str(zip_path), "--yes", "--no-models")
    assert result.returncode == 1
    assert "exists and is not Sombra" in result.stderr
    assert (other / "Info.plist").is_file()


def test_refuses_a_bad_signature_or_bundle_id(mac: dict[str, Path]) -> None:
    zip_path = _make_zip(mac["tmp"] / "Sombra-0.2.0-macos-arm64.zip")
    result = _install(
        mac, "--from-app-zip", str(zip_path), "--yes", "--no-models", FAKE_CODESIGN_RC="1"
    )
    assert result.returncode == 1
    assert "code signature does not verify" in result.stderr
    assert not (mac["apps"] / "Sombra.app").exists()

    wrong = _make_zip(mac["tmp"] / "Sombra-0.2.1-macos-arm64.zip", bundle_id="com.evil.Sombra")
    result = _install(mac, "--from-app-zip", str(wrong), "--yes", "--no-models")
    assert result.returncode == 1
    assert "not io.github.nickmaglowsch.Sombra" in result.stderr


def _release(mac: dict[str, Path], with_app: bool) -> str:
    rel = mac["tmp"] / "release"
    rel.mkdir()
    wheel = rel / "sombra-0.2.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    names = [wheel.name]
    if with_app:
        names.append(_make_zip(rel / "Sombra-0.2.0-macos-arm64.zip").name)
    sums = "".join(f"{hashlib.sha256((rel / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
    (rel / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return rel.as_uri()


def test_release_with_an_app_installs_the_app(mac: dict[str, Path]) -> None:
    base = _release(mac, with_app=True)
    result = _install(mac, "--yes", "--no-models", SOMBRA_RELEASE_BASE_URL=base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sha256 ok: Sombra-0.2.0-macos-arm64.zip" in result.stderr
    assert (mac["apps"] / "Sombra.app").is_dir()
    assert _link(mac).is_symlink()


def test_release_without_an_app_falls_back_to_uv(mac: dict[str, Path]) -> None:
    base = _release(mac, with_app=False)
    result = _install(mac, "--yes", "--no-models", SOMBRA_RELEASE_BASE_URL=base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "has no Sombra.app; installing with uv instead" in result.stdout
    assert "tool install" in (mac["home"] / "uv.log").read_text(encoding="utf-8")
    assert not (mac["apps"] / "Sombra.app").exists()


def test_no_app_uses_uv(mac: dict[str, Path]) -> None:
    base = _release(mac, with_app=True)
    result = _install(mac, "--no-app", "--yes", "--no-models", SOMBRA_RELEASE_BASE_URL=base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tool install" in (mac["home"] / "uv.log").read_text(encoding="utf-8")
    assert not (mac["apps"] / "Sombra.app").exists()
    assert "to the terminal" in result.stdout


def test_uninstall_removes_the_app_and_link_but_keeps_meetings(mac: dict[str, Path]) -> None:
    zip_path = _make_zip(mac["tmp"] / "Sombra-0.2.0-macos-arm64.zip")
    assert _install(mac, "--from-app-zip", str(zip_path), "--yes", "--no-models").returncode == 0
    meeting = mac["home"] / "Sombra" / "meetings" / "2026-01-01_0900_x" / "transcript.md"
    meeting.parent.mkdir(parents=True)
    meeting.write_text("[09:00:00] EU: oi\n")
    result = _install(mac, "--uninstall", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (mac["apps"] / "Sombra.app").exists()
    assert not _link(mac).is_symlink()
    assert f"tccutil reset All {sombra_app.BUNDLE_ID}" in result.stdout
    assert meeting.exists()


def test_from_app_zip_is_macos_only(mac: dict[str, Path]) -> None:
    (mac["fakebin"] / "uname").write_text(
        '#!/bin/sh\ncase "$1" in -m) echo x86_64 ;; *) echo Linux ;; esac\n', encoding="utf-8"
    )
    zip_path = _make_zip(mac["tmp"] / "Sombra-0.2.0-macos-arm64.zip")
    result = _install(mac, "--from-app-zip", str(zip_path), "--yes", "--no-models")
    assert result.returncode == 1
    assert "--from-app-zip is for macOS only" in result.stderr
