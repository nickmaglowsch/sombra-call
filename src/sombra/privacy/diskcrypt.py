"""Disk-encryption check: warn at start if meetings would be stored on an unencrypted disk.

macOS: ``fdesetup status`` (FileVault). Linux: is there a ``crypt`` (LUKS/dm-crypt)
device under the filesystem holding the meetings root (``findmnt`` + ``lsblk``).
The parsers are pure functions; the command runner is injectable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

Runner = Callable[[Sequence[str]], str]  # argv -> stdout; raises OSError/CalledProcessError


class EncryptionState(StrEnum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EncryptionStatus:
    state: EncryptionState
    detail: str

    @property
    def warning(self) -> str | None:
        """A user-facing PT-BR warning, or None when the disk is encrypted."""
        if self.state is EncryptionState.ON:
            return None
        if self.state is EncryptionState.OFF:
            return (
                "Aviso: o disco não está criptografado "
                f"({self.detail}). Gravações de reuniões ficam legíveis por quem tiver o disco; "
                "ative o FileVault (macOS) ou LUKS (Linux)."
            )
        return f"Aviso: não foi possível verificar a criptografia do disco ({self.detail})."


def _run(argv: Sequence[str]) -> str:
    exe = shutil.which(argv[0])
    if exe is None:
        raise FileNotFoundError(argv[0])
    return subprocess.run(  # noqa: S603 - fixed argv from this module, no shell
        [exe, *argv[1:]], capture_output=True, text=True, check=True, timeout=5
    ).stdout


def parse_fdesetup(output: str) -> EncryptionState:
    text = output.strip().lower()
    if "filevault is on" in text:
        return EncryptionState.ON
    if "filevault is off" in text:
        return EncryptionState.OFF
    # "Encryption in progress" / "Decryption in progress": not yet (or no longer) safe.
    if "in progress" in text:
        return EncryptionState.OFF
    return EncryptionState.UNKNOWN


def parse_lsblk_types(output: str) -> EncryptionState:
    """``lsblk -s -n -o TYPE <device>`` lists the device and every ancestor, one type per line."""
    types = {line.strip() for line in output.splitlines() if line.strip()}
    if not types:
        return EncryptionState.UNKNOWN
    return EncryptionState.ON if "crypt" in types else EncryptionState.OFF


def check_disk_encryption(
    path: Path | None = None, *, platform: str = sys.platform, runner: Runner = _run
) -> EncryptionStatus:
    """Encryption state of the disk holding ``path`` (default: home). Never raises."""
    target = path if path is not None else Path.home()
    try:
        if platform == "darwin":
            state = parse_fdesetup(runner(["fdesetup", "status"]))
            return EncryptionStatus(state, "FileVault")
        if platform.startswith("linux"):
            source = runner(["findmnt", "-n", "-o", "SOURCE", "--target", str(target)]).strip()
            if not source.startswith("/dev/"):
                return EncryptionStatus(EncryptionState.UNKNOWN, f"not a block device: {source}")
            state = parse_lsblk_types(runner(["lsblk", "-s", "-n", "-o", "TYPE", source]))
            return EncryptionStatus(state, f"LUKS on {source}")
    except (OSError, subprocess.SubprocessError) as exc:
        return EncryptionStatus(EncryptionState.UNKNOWN, type(exc).__name__)
    return EncryptionStatus(EncryptionState.UNKNOWN, f"unsupported platform {platform}")
