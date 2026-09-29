import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from sombra.privacy import diskcrypt
from sombra.privacy.diskcrypt import (
    EncryptionState,
    EncryptionStatus,
    check_disk_encryption,
    parse_fdesetup,
    parse_lsblk_types,
)


@pytest.mark.parametrize(
    ("out", "state"),
    [
        ("FileVault is On.\n", EncryptionState.ON),
        ("FileVault is Off.\n", EncryptionState.OFF),
        ("Encryption in progress: Percent completed = 12.3\n", EncryptionState.OFF),
        ("weird", EncryptionState.UNKNOWN),
    ],
)
def test_parse_fdesetup(out: str, state: EncryptionState) -> None:
    assert parse_fdesetup(out) is state


@pytest.mark.parametrize(
    ("out", "state"),
    [
        ("part\ncrypt\npart\ndisk\n", EncryptionState.ON),
        ("lvm\ncrypt\npart\ndisk\n", EncryptionState.ON),
        ("part\ndisk\n", EncryptionState.OFF),
        ("\n", EncryptionState.UNKNOWN),
    ],
)
def test_parse_lsblk(out: str, state: EncryptionState) -> None:
    assert parse_lsblk_types(out) is state


def fake_runner(outputs: dict[str, str]) -> diskcrypt.Runner:
    def run(argv: Sequence[str]) -> str:
        return outputs[argv[0]]

    return run


def test_macos_filevault_off_warns() -> None:
    status = check_disk_encryption(
        platform="darwin", runner=fake_runner({"fdesetup": "FileVault is Off."})
    )
    assert status.state is EncryptionState.OFF
    assert status.warning is not None
    assert "FileVault" in status.warning


def test_macos_filevault_on_no_warning() -> None:
    status = check_disk_encryption(
        platform="darwin", runner=fake_runner({"fdesetup": "FileVault is On."})
    )
    assert status.warning is None


def test_linux_luks() -> None:
    runner = fake_runner({"findmnt": "/dev/mapper/root\n", "lsblk": "crypt\npart\ndisk\n"})
    status = check_disk_encryption(Path("/home"), platform="linux", runner=runner)
    assert status == EncryptionStatus(EncryptionState.ON, "LUKS on /dev/mapper/root")


def test_linux_not_block_device() -> None:
    status = check_disk_encryption(platform="linux", runner=fake_runner({"findmnt": "overlay\n"}))
    assert status.state is EncryptionState.UNKNOWN
    assert status.warning is not None


def test_command_failure_is_unknown_never_raises() -> None:
    def fail(argv: Sequence[str]) -> str:
        raise subprocess.CalledProcessError(1, list(argv))

    status = check_disk_encryption(platform="darwin", runner=fail)
    assert status.state is EncryptionState.UNKNOWN


def test_unsupported_platform() -> None:
    assert check_disk_encryption(platform="win32").state is EncryptionState.UNKNOWN


def test_default_runner_missing_binary() -> None:
    with pytest.raises(FileNotFoundError):
        diskcrypt._run(["definitely-not-a-real-binary-xyz"])


def test_default_runner_runs() -> None:
    assert diskcrypt._run(["echo", "hi"]).strip() == "hi"


@pytest.mark.hardware
def test_real_disk_check() -> None:
    status = check_disk_encryption()
    assert status.state in set(EncryptionState)
