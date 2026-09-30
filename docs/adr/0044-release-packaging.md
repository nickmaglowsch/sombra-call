# ADR 0044: Ship a wheel installed with `uv tool install` on uv-managed CPython 3.12

- Status: **accepted**
- Date: 2026-09-29
- Issue: #44 (epic #43, R1)
- Numbering: this ADR uses the issue number, as ADR 0009 and 0018 do.

## Context

The epic (#43) wants anyone on macOS or Ubuntu to install Sombra with one command. The installer (#45, R2) downloads what a release publishes, so this ADR decides what a release publishes. It covers the formats the issue lists:

- **(a)** a wheel installed with `uv tool install` on a uv-managed CPython 3.12;
- **(b)** a self-contained bundle: PyInstaller or Briefcase, up to a signed and notarized macOS `.app`;
- **(c)** a Homebrew tap and/or a `.deb`.

We judge them on the native dependencies, macOS privacy permissions (TCC), signing, upgrade and uninstall, and CI cost.

### Evidence: native dependencies

Sombra itself is pure Python (`py3-none-any`). The question is whether its native dependencies install from wheels on every target or need a compiler. The versions below are the ones `uv.lock` pins on 2026-09-29. The file lists come from `https://pypi.org/pypi/<name>/<version>/json`, filtered to CPython 3.12.

| Package (pinned) | macOS arm64 | macOS x86_64 | manylinux x86_64 | manylinux aarch64 | sdist |
| --- | --- | --- | --- | --- | --- |
| `pywhispercpp` 1.5.1 | `macosx_11_0_arm64` | **none** | `manylinux_2_28_x86_64` | `manylinux_2_28_aarch64` | yes |
| `onnxruntime` 1.30.0 | `macosx_14_0_arm64` | **none** | `manylinux_2_28_x86_64` | `manylinux_2_28_aarch64` | **no** |
| `pyobjc-core` + `pyobjc-framework-{cocoa,coreaudio,coremedia,quartz,screencapturekit}` 12.2.2 | `macosx_10_13_universal2` | `macosx_10_13_universal2` | n/a (darwin marker) | n/a | yes |
| `sounddevice` 0.5.6 (darwin only) | `py3-none-macosx_10_6_x86_64.macosx_10_6_universal2` (bundles PortAudio) | same | n/a (darwin marker) | n/a | yes |
| `numpy` 2.5.3, `pillow` 12.3.0, `rapidfuzz` 3.14.6, `aiohttp` 3.14.3 | wheels | wheels | wheels | wheels | yes |

Resolving the whole project with `--only-binary :all:` confirms it. Each command is `uv pip compile pyproject.toml --python-version 3.12 --only-binary :all: --python-platform <target>`:

- `aarch64-apple-darwin` with `MACOSX_DEPLOYMENT_TARGET=14.0`: **resolves**, wheels only.
- `aarch64-apple-darwin` with uv's default deployment target (13.0): **fails**. onnxruntime has "no wheels with a matching platform tag (e.g., `macosx_13_0_arm64`)".
- `x86_64-apple-darwin`, even with a 14.0 target: **fails**, on onnxruntime again. Allowing source builds does not help, because onnxruntime 1.30.0 publishes no sdist.
- `x86_64-manylinux_2_28` and `aarch64-manylinux_2_28`: **resolve**, wheels only.

Two more facts:

- **onnxruntime dropped Intel Macs.** The last onnxruntime release with a `macosx_*_x86_64` CPython 3.12 wheel is 1.23.2 (`macosx_13_0_x86_64`). Since 1.24 it ships only `macosx_14_0_arm64` on macOS. Our floor is `onnxruntime>=1.30.0`.
- **pywhispercpp would compile on an Intel Mac**, from its sdist. Its `build-system` needs `cmake>=3.12`, `ninja`, `setuptools-scm` and `repairwheel` (all from PyPI) plus a C/C++ compiler (Xcode command-line tools). It is moot while onnxruntime blocks the platform.

So nothing compiles on a supported target. The supported targets are **macOS 14+ on Apple Silicon** and **Linux with glibc ≥ 2.28 on x86_64 and aarch64**. That glibc floor covers Ubuntu 20.04 (2.31), 22.04 (2.35) and 24.04 (2.39). Intel Macs are unsupported: supporting them means pinning `onnxruntime<1.24` or dropping it for another VAD, which is a transcription decision outside this issue.

### Evidence: macOS TCC

macOS attributes Microphone, Screen Recording and Accessibility to the **responsible process**. For a command-line tool started from a shell, that is the terminal app that launched it: Terminal, iTerm2, VS Code. The repo's docs already say so, from the capture modules' manual testing (`docs/macos-screen.md`, `docs/macos-audio.md`, `docs/privacy.md`). The `python3.12` binary under `~/.local/share/uv/python/` never appears in System Settings as long as a terminal launches it.

- **(a) wheel + uv:** the user grants the permissions to their terminal app. The grant survives every Sombra and Python upgrade, because the terminal's code signature doesn't change. The catch is scope: every program run in that terminal gets the same permissions. Background launch with no terminal (launchd, a login item) would make the Python binary the responsible process. uv's CPython builds carry no Developer ID signature, so TCC would key the grant on a code hash that changes with every Python upgrade. We don't launch from launchd, so this doesn't arise today.
- **(b) signed `.app`:** the grant attaches to Sombra's bundle id and Developer ID team. It survives upgrades as long as the bundle id and signing identity stay the same, and it is scoped to Sombra alone. This is the right end state. It needs the hardened runtime, entitlements such as `com.apple.security.device.audio-input`, a signature on every bundled dylib (onnxruntime, whisper.cpp, PortAudio, pyobjc extensions), and notarization. An unsigned PyInstaller or Briefcase bundle is worse than (a): Gatekeeper quarantines it, and its ad-hoc identity changes with every build, so users would re-grant after each upgrade.
- **(c) Homebrew / `.deb`:** a Homebrew formula installs a CLI, so the grants go to the terminal exactly as in (a). A `.deb` is Linux-only, and Linux has no TCC.

## Options

### (a) Wheel + `uv tool install` on uv-managed CPython 3.12 (chosen)

- **Native deps:** all wheels on the supported targets, as shown above. The uv-managed CPython means we never depend on the system Python: Ubuntu 22.04 ships 3.10, and macOS ships none suitable.
- **TCC:** grants go to the terminal and survive upgrades. That matches today's docs.
- **Signing:** none needed. The wheel holds no binaries of ours, and the native wheels come from PyPI already signed or ad-hoc signed by their maintainers. uv's downloads are not quarantined, so Gatekeeper does not block them.
- **Upgrade:** re-run the installer, or `uv tool install --force <new wheel>`. uv rebuilds the tool's environment, and user data lives outside it. `uv tool upgrade` does not apply, because the tool was installed from a file or URL, not an index.
- **Uninstall:** `uv tool uninstall sombra`. Config (`~/.config/sombra`), models (`~/.cache/sombra/models`) and meetings (`~/Sombra/meetings`) stay unless the user deletes them. #45's `--uninstall` owns that flow.
- **CI cost:** one Ubuntu build (about 1 min), plus a four-runner smoke matrix of a few minutes each. The repo is public, so standard GitHub-hosted runners (macOS and `ubuntu-24.04-arm` included) cost nothing.
- **Not PyPI.** The name `sombra` on PyPI belongs to an unrelated project ("Python bindings for the Sombra graph database", 0.4.0). So `uv tool install sombra` would install the wrong thing. Releases are GitHub Releases only. The installer and docs always install a specific wheel file or URL, verified against `SHA256SUMS`.

### (b) Self-contained bundle (PyInstaller / Briefcase / signed `.app`)

- **Native deps:** the bundler vendors the same wheels, so availability is the same as (a). But every release then builds per architecture on its own runner (arm64 macOS, x86_64 and aarch64 Linux), because PyInstaller does not cross-build.
- **TCC:** best, but only when signed and notarized; see above. Unsigned, it is worse than (a).
- **Signing:** an Apple Developer Program membership (USD 99/year), a Developer ID Application certificate and a notarytool API key, all stored as repository secrets. The build must sign every nested Mach-O, which PyInstaller's `--codesign-identity` and Briefcase's `briefcase package` handle with care around dylibs. Notarization runs on each release (minutes to hours in Apple's queue).
- **Upgrade and uninstall:** drag-and-drop or Sparkle for the app, while the CLI needs a symlink into `PATH`. That is more moving parts than (a).
- **CI cost:** a macOS build job per release, plus notarization waits. Free on a public repo, but the slowest option.
- **Verdict:** this is where the macOS product should go, because permissions scoped to Sombra matter for a tool that records calls. But it is a project of its own, with an account, secrets and a signing pipeline, and it blocks nothing in the MVP. Filed as #50.

### (c) Homebrew tap and/or `.deb`

- **Homebrew:** a tap formula for a Python app declares every dependency as a `resource`. `virtualenv_install_with_resources` then builds them into a Homebrew-managed Python. homebrew-core policy builds from source, and onnxruntime has no sdist, so that is out. A private tap could install our wheel plus PyPI wheels, but at that point it is a wrapper around (a) with the same TCC story and one more repository to keep in sync. `brew upgrade` and `brew uninstall` are the nice part.
- **`.deb`:** it would need `dh-virtualenv` or a vendored venv, since Ubuntu 22.04's Python is 3.10. It would need an apt repository or a manual `dpkg -i`, and per-architecture builds. That is a lot of packaging for no gain over (a) on Linux, where there is no TCC.
- **Verdict:** a Homebrew tap that installs the released wheel is a cheap convenience once releases are stable. Filed as #51. No `.deb`.

## Decision

1. **A release is a GitHub Release** with these assets: the wheel (`sombra-X.Y.Z-py3-none-any.whl`), the sdist, `install.sh` (once #45 lands) and `SHA256SUMS`. There is one wheel for all platforms: it is pure Python, and the platform-specific wheels come from PyPI at install time, pinned only by our lower bounds.
2. **Users install it with `uv tool install`** on a uv-managed CPython 3.12, through `install.sh` (#45).
3. **Supported targets:** macOS 14+ on arm64, and Ubuntu 22.04/24.04 (glibc ≥ 2.28) on x86_64 and aarch64. The release workflow smoke-installs on `macos-14`, `ubuntu-latest` (24.04), `ubuntu-22.04` and `ubuntu-24.04-arm`. Intel Macs are not supported, because onnxruntime ships no wheel or sdist for them.
4. **The version comes from the tag** through hatch-vcs, so a release needs no commit on the protected `main` (see `docs/release.md`).
5. **macOS permissions go to the terminal app.** `sombra doctor` (#45) says so. A signed `.app` that owns its own grants is the follow-up.

## Consequences

- Installing needs network access to PyPI for the dependencies, and to GitHub for uv's CPython. Offline installs are out of scope.
- Dependencies are resolved at install time, not frozen: two users installing the same release on different days can get different patch versions of, say, numpy. If that bites, we will publish a constraints file generated from `uv.lock` as another release asset. This is noted rather than done now, because every dependency above ships wheels for all the targets and is pinned from below.
- Granting Screen Recording and Accessibility to a terminal is broader than users may like. The docs and `doctor` must say which app gets the grant and why, until the `.app` follow-up lands.
- Raising `onnxruntime` or `pywhispercpp` must keep wheels for all four smoke targets. The release workflow's PR run on `uv.lock` changes catches a regression before merge.

## Follow-ups

- Signed and notarized macOS `.app`, so the TCC grants belong to Sombra: #50, decided in [ADR 0050](0050-macos-app-bundle.md).
- A Homebrew tap that installs the released wheel: #51.
- `sombra replay` with the fake brain in the smoke job: done once #17 merged (the e2e fixture, whisper tiny).
