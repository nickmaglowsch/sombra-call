# PyInstaller spec for Sombra.app (ADR 0050). Run through build.py, never by hand:
# build.py installs the locked dependencies and the release wheel into a clean venv,
# sets SOMBRA_VERSION, and adds the localised strings and the CLI shim afterwards.
#
# onedir, not onefile: onefile unpacks to a temp dir on every start, which breaks the
# code signature and TCC identity. The bundle's main executable is PyInstaller's
# bootloader (Contents/MacOS/Sombra); it runs Python in-process, so it is the process
# TCC sees once the shim disclaims responsibility.

import os
import sys

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)

sys.path.insert(0, SPECPATH)
import sombra_app as app  # packaging/macos/sombra_app.py

version = os.environ["SOMBRA_VERSION"]

# sombra.cli discovers `<pkg>.commands` with pkgutil and the code imports platform
# bindings lazily through importlib, which static analysis cannot see: list them.
hiddenimports = collect_submodules("sombra") + [
    "objc",
    "Foundation",
    "AppKit",
    "Quartz",
    "CoreAudio",
    "CoreMedia",
    "ScreenCaptureKit",
    "PyObjCTools.AppHelper",
    "sounddevice",
    "keyring",
    "keyring.backends.macOS",
]
datas = (
    collect_data_files("sombra")  # prompts, UI static files
    + copy_metadata("sombra")  # importlib.metadata.version("sombra") -> `sombra --version`
    + copy_metadata("keyring")  # keyring finds its backends through entry points
    + collect_data_files("pywhispercpp")
)
binaries = collect_dynamic_libs("pywhispercpp")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "webview"],  # the pywebview window is an optional extra
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=app.EXECUTABLE,
    console=True,
    argv_emulation=False,
    target_arch="arm64" if sys.platform == "darwin" else None,
    codesign_identity=None,  # sign.py signs the finished bundle, inside-out
    entitlements_file=None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name=app.APP_NAME, upx=False)

if sys.platform == "darwin":
    app_bundle = BUNDLE(
        coll,
        name=app.APP_DIR,
        bundle_identifier=app.BUNDLE_ID,
        version=version,
        info_plist=app.info_plist(version),
    )
