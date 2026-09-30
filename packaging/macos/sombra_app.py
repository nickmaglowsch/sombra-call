"""What ``Sombra.app`` is: bundle id, layout, ``Info.plist`` and its PT-BR/EN usage strings.

The one place these facts live (ADR 0050). ``sombra.spec`` (PyInstaller), ``build.py``,
``sign.py``, the release gate and the tests all read them from here, and ``install.sh``
and ``sombra doctor`` repeat the bundle id (a test checks they agree).

Standard library only: it runs before anything is installed.
"""

from __future__ import annotations

import re
from typing import Any

# Stable forever: TCC keys the Microphone / Screen Recording / Accessibility grants on the
# bundle id plus the Developer ID team. Changing it makes every user grant them again.
BUNDLE_ID = "io.github.nickmaglowsch.Sombra"
APP_NAME = "Sombra"
APP_DIR = f"{APP_NAME}.app"
EXECUTABLE = "Sombra"  # Contents/MacOS/Sombra: PyInstaller's bootloader, the TCC identity
SHIM_RELPATH = "Contents/Helpers/sombra"  # the CLI shim users run (symlinked onto PATH)
SHIM_ID = f"{BUNDLE_ID}.cli"  # the shim's own code-signing identifier
MIN_MACOS = "14.0"

# The release asset #51's Homebrew cask downloads; listed in SHA256SUMS.
ZIP_TEMPLATE = "Sombra-{version}-macos-arm64.zip"
_ZIP = re.compile(r"^Sombra-(?P<version>[0-9][0-9A-Za-z.+]*)-macos-arm64\.zip$")

# Info.plist usage strings. macOS shows them in its permission prompts, localised by
# ``<lang>.lproj/InfoPlist.strings``; the English text is also the Info.plist default.
USAGE: dict[str, dict[str, str]] = {
    "en": {
        "NSMicrophoneUsageDescription": (
            "Sombra transcribes your voice during meetings, on this Mac. "
            "Audio never leaves your computer."
        ),
        "NSAudioCaptureUsageDescription": (
            "Sombra records the other participants' audio (system audio) to transcribe "
            "the meeting on this Mac."
        ),
        "NSScreenCaptureUsageDescription": (
            "Sombra reads the shared screen and window titles so it can answer questions "
            "about what was shown in the meeting."
        ),
    },
    "pt-BR": {
        "NSMicrophoneUsageDescription": (
            "O Sombra transcreve a sua voz durante as reuniões, neste Mac. "
            "O áudio nunca sai do seu computador."
        ),
        "NSAudioCaptureUsageDescription": (
            "O Sombra grava o áudio dos outros participantes (áudio do sistema) para "
            "transcrever a reunião neste Mac."
        ),
        "NSScreenCaptureUsageDescription": (
            "O Sombra lê a tela compartilhada e os títulos das janelas para responder "
            "perguntas sobre o que foi mostrado na reunião."
        ),
    },
}
DEVELOPMENT_REGION = "en"


def zip_name(version: str) -> str:
    """``Sombra-0.2.0-macos-arm64.zip``: the release asset name for ``version``."""
    if not version or "/" in version or "-" in version:
        raise ValueError(f"not a release version: {version!r}")
    return ZIP_TEMPLATE.format(version=version)


def zip_version(name: str) -> str | None:
    """The version in a release zip's file name, or None if it is not one."""
    m = _ZIP.match(name)
    return m["version"] if m else None


def info_plist(version: str) -> dict[str, Any]:
    """The ``Info.plist`` keys we set (PyInstaller fills in the executable and icon ones)."""
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        # The tag's version (PEP 440, what `sombra --version` prints); #51 relies on it.
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleDevelopmentRegion": DEVELOPMENT_REGION,
        "CFBundleLocalizations": list(USAGE),
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSArchitecturePriority": ["arm64"],
        # A command-line app: no Dock icon or menu bar when it runs.
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        **USAGE[DEVELOPMENT_REGION],
    }


def _strings_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def infoplist_strings(lang: str) -> str:
    """``<lang>.lproj/InfoPlist.strings`` (UTF-8 text format, which CFBundle accepts)."""
    lines = [f"/* {APP_NAME}: permission prompts ({lang}). Generated from sombra_app.py. */"]
    lines += [f'"{key}" = "{_strings_escape(text)}";' for key, text in USAGE[lang].items()]
    return "\n".join(lines) + "\n"
