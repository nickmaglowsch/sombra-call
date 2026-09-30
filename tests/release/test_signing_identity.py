"""``scripts/release/make-signing-identity.sh``: the one-time self-signed identity (ADR 0050).

``import-signing-identity.sh`` needs macOS' ``security`` and runs in the release
workflow's app job, with a throwaway identity this script makes.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "release"
MAKE = SCRIPTS / "make-signing-identity.sh"
IMPORT = SCRIPTS / "import-signing-identity.sh"

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # our script, fixed argv
        ["sh", str(MAKE), *args],  # noqa: S607  # POSIX sh
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _openssl(*args: str) -> str:
    return subprocess.run(  # noqa: S603  # fixed argv
        ["openssl", *args],  # noqa: S607  # found on PATH (skip marker)
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
@pytest.mark.parametrize("script", [MAKE, IMPORT])
def test_scripts_are_shellcheck_clean(script: Path) -> None:
    result = subprocess.run(  # noqa: S603  # fixed argv
        ["shellcheck", "--shell=sh", str(script)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_openssl
def test_makes_a_code_signing_identity_for_ci(tmp_path: Path) -> None:
    out = tmp_path / "github-output"
    result = _run(
        "--out",
        str(tmp_path / "id"),
        "--name",
        "Sombra Test",
        "--days",
        "2",
        "--github-output",
        str(out),
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert set(values) == {"p12", "cer", "password", "sha256", "sha1"}
    cer = Path(values["cer"])
    der = cer.read_bytes()
    assert values["sha256"] == hashlib.sha256(der).hexdigest()
    assert values["sha1"] == hashlib.sha1(der).hexdigest()  # noqa: S324  # a fingerprint
    text = _openssl("x509", "-inform", "DER", "-in", str(cer), "-noout", "-text")
    assert "Code Signing" in text  # extendedKeyUsage=codeSigning
    assert "CA:FALSE" in text
    assert "CN=Sombra Test" in text.replace(" = ", "=")
    # The .p12 opens with the printed password and holds the same certificate.
    pem = _openssl(
        *("pkcs12", "-in", values["p12"], "-passin", f"pass:{values['password']}"),
        *("-nokeys", "-clcerts"),
    )
    assert "BEGIN CERTIFICATE" in pem
    assert "Sombra Test" in pem  # friendlyName bag attribute
    # In CI mode the password only appears in the ::add-mask:: command that hides it.
    shown = [line for line in result.stdout.splitlines() if values["password"] in line]
    assert shown == [f"::add-mask::{values['password']}"]


@needs_openssl
def test_owner_mode_prints_the_secrets_and_the_pin(tmp_path: Path) -> None:
    result = _run("--out", str(tmp_path / "id"))
    assert result.returncode == 0, result.stderr
    assert "MACOS_SELFSIGN_P12_BASE64" in result.stdout
    assert "MACOS_SELFSIGN_P12_PASSWORD" in result.stdout
    assert "packaging/macos/signing-cert.sha256" in result.stdout
    der = (tmp_path / "id" / "sombra-signing.cer").read_bytes()
    assert hashlib.sha256(der).hexdigest() in result.stdout
    # Never overwrites an identity.
    again = _run("--out", str(tmp_path / "id"))
    assert again.returncode == 1
    assert "refusing to overwrite" in again.stderr


def test_bad_arguments() -> None:
    assert "--days must be a number" in _run("--days", "soon").stderr
    assert "unknown argument" in _run("--bogus").stderr
