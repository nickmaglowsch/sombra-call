# ADR 0050: A signed and notarized `Sombra.app`, built with PyInstaller, owns the macOS permissions

- Status: **provisional**. The bundle, the CLI shim, the ad-hoc signature and the release gate are built and exercised in CI on `macos-14` (see [Evidence](#evidence-from-ci)). What needs Apple credentials or a person at a real Mac (Developer ID signing, notarization, the System Settings entries, grants surviving an upgrade) waits for a human run. See [Left for a human](#left-for-a-human).
- Date: 2026-09-29
- Issue: #50 (epic #43; follow-up from ADR 0044). The Homebrew cask (#51) consumes the release asset defined here.
- Numbering: this ADR uses the issue number, as ADRs 0009, 0018, 0044 and 0046 do.

## Context

Releases ship a wheel installed with `uv tool install` (ADR 0044). macOS then attributes Microphone, Screen Recording and Accessibility to the **responsible process**. For a command-line tool that is the terminal app it runs in, so the grant covers every program run from that terminal. A `.app` with a stable bundle id, signed by a Developer ID team, gets grants that belong to Sombra alone and survive upgrades.

Three things have to hold together:

1. a bundler that packs Sombra's native dependencies into a `.app`: onnxruntime, pywhispercpp (whisper.cpp), PortAudio through sounddevice, and the pyobjc frameworks (Cocoa, CoreAudio, Quartz, ScreenCaptureKit);
2. a way for `sombra` typed in a terminal to run **as Sombra** in TCC's eyes, not as the terminal;
3. a signing pipeline, and a gate that stops a final release from shipping without it.

## How TCC decides who asks

`tccd` does not look at the process that calls `AVCaptureDevice` or `CGPreflightScreenCaptureAccess`. It looks at that process's *responsible process*, which is inherited across `fork`/`exec`. A GUI app started by LaunchServices is responsible for itself. A shell's children, and everything they exec, stay the responsibility of the terminal app. That is why the uv install's grants go to Terminal and why running `Sombra.app/Contents/MacOS/Sombra` straight from a shell would change nothing.

The spawn attribute `responsibility_spawnattrs_setdisclaim(attr, 1)` makes a `posix_spawn`ed child responsible for itself. It is private SPI in libSystem. LLDB uses it to launch debuggees, Chromium uses it for its helpers, and Qt Creator uses it for the programs it runs, all for this exact TCC reason. The matching query, `responsibility_get_pid_responsible_for_pid`, is what `sombra doctor` calls to report who holds the grants.

What TCC stores for a client is its bundle id plus a code requirement, the designated requirement of its signature:

- For a **Developer ID** app that is "identifier `io.github.nickmaglowsch.Sombra`, and anchor Apple generic, and team `<TEAM>`". It is the same for every version signed by the same team, so a grant survives upgrades.
- For an **ad-hoc** signature it is the code hash (cdhash), which changes with every build, so the user would grant again after each upgrade. That is why an ad-hoc app is never a release asset.

How to see it on a Mac, for the human check below:

```sh
# Who does TCC attribute a request to? Watch while running `sombra doctor` or a meeting:
log stream --debug --predicate 'subsystem == "com.apple.TCC"' | grep -i -E 'AttributionChain|sombra'
#   ... responsible={TCCDProcess: identifier=io.github.nickmaglowsch.Sombra, ... binary_path=/Applications/Sombra.app/Contents/MacOS/Sombra}
# Remove Sombra's grants, to test the prompts again:
tccutil reset All io.github.nickmaglowsch.Sombra
```

## Options: the bundler

| | PyInstaller 6.22 (+ hooks-contrib 2026.7) | Briefcase | py2app |
| --- | --- | --- | --- |
| Input | the same locked wheels `uv.lock` pins, installed into a clean venv on a uv-managed CPython 3.12 | its own app template and "support package" Python, deps installed by pip from `pyproject` | a setuptools build of the source tree |
| onnxruntime | hook in hooks-contrib (collects its dylibs) | pip wheel copied as is | no hook; manual `includes`/`frameworks` |
| sounddevice / PortAudio | hook in hooks-contrib (collects `_sounddevice_data/portaudio-binaries/libportaudio.dylib`) | wheel copied as is | manual |
| pyobjc frameworks | hooks for `objc`, `Foundation`, `AppKit`, `Quartz`, `CoreAudio`, `ScreenCaptureKit`, … | wheel copied as is | first-class (same authors as pyobjc) |
| pywhispercpp | no hook needed: binary analysis follows the extension's `@rpath` links (`libwhisper`, `libggml*`); the spec also collects its libs and data | wheel copied as is | manual |
| Signing | we sign ourselves, inside-out (below) | `briefcase package` signs and notarizes, its own way | we sign ourselves |
| Fits our CLI | `console=True` onedir `.app`, no Dock icon | built around a GUI app; a CLI needs workarounds | GUI-first |
| License | GPL-2.0 with the bootloader exception: bundling proprietary apps is allowed | BSD-3 | MIT |

**Chosen: PyInstaller, onedir, wrapped in a `.app` by its `BUNDLE` step.** It is the only one we built. The decision rests on that build (below) plus the table: it starts from the wheels the lock pins, so the app and the wheel run the same code and the same dependency versions; every native dependency has a hook or needs none; and it lets us control signing, which the release gate depends on. Briefcase would probably work too (it copies wheels as is), but it brings its own Python build and template, and its signing pipeline would stand between us and the gate. py2app knows pyobjc best but needs manual work for onnxruntime and PortAudio. Neither was built, so neither is "proven not to work"; they lost on fit.

`onefile` is out: it unpacks to a temporary folder on every start, which breaks the code signature and the TCC identity.

## Decision

### The bundle

- **Bundle id `io.github.nickmaglowsch.Sombra`, forever.** Reverse DNS of a domain the project controls (its GitHub Pages namespace). Changing it later would make every user grant again, so it lives in one place (`packaging/macos/sombra_app.py`), and a test checks that `install.sh` and `sombra doctor` agree with it.
- **Layout.** `Contents/MacOS/Sombra` is PyInstaller's bootloader. It runs Python in-process, so its pid is the one TCC sees. Python, extension modules and dylibs sit in `Contents/Frameworks`, data (prompts, UI files, `sombra`'s dist-info for `--version`) in `Contents/Resources`, and the CLI shim in `Contents/Helpers/sombra` (Apple's place for helper tools; `Contents/MacOS/sombra` would clash with `Sombra` on a case-insensitive disk).
- **`Info.plist`.** `CFBundleShortVersionString` = `CFBundleVersion` = the release version exactly as `sombra --version` prints it (PEP 440: `0.2.0`, `0.2.0rc1`); #51 relies on it. `LSMinimumSystemVersion` 14.0, `LSArchitecturePriority` arm64, and `LSUIElement` true (a command-line app gets no Dock icon).
- **Usage strings, EN and PT-BR.** `NSMicrophoneUsageDescription` (microphone), `NSAudioCaptureUsageDescription` (the Core Audio process tap, "System Audio Recording", macOS 14.4+) and `NSScreenCaptureUsageDescription`. English is in `Info.plist`, and each language has a `<lang>.lproj/InfoPlist.strings` (`en`, `pt-BR`). Apple documents no usage key for Screen Recording on macOS 14; its prompt text is fixed. We ship the key anyway because macOS ignores keys it doesn't read. Accessibility has no usage key.
- **Excluded:** the optional `pywebview` window (the `window` extra) and tkinter.

### The CLI shim

`Contents/Helpers/sombra` is ~130 lines of C (`packaging/macos/shim/sombra.c`), built with `clang -arch arm64 -mmacosx-version-min=14.0`. `install.sh` links it onto `PATH` (`~/.local/bin/sombra`, uv's tool directory too), and the Homebrew cask's `binary` stanza (#51) points at the same path. When run, the shim:

1. resolves its real path through the symlink (`_NSGetExecutablePath` + `realpath`) and finds `../MacOS/Sombra`;
2. `posix_spawn`s it with `responsibility_spawnattrs_setdisclaim(attr, 1)`, looked up with `dlsym` so a macOS that drops the SPI still runs Sombra, just with the terminal's grants, which `doctor` reports;
3. passes argv, the environment and stdin/stdout/stderr through unchanged, resets signal handlers and the mask for the child, forwards `SIGTERM`/`SIGHUP`/`SIGUSR1`/`SIGUSR2` (Ctrl-C already reaches the child through the process group), and exits with the child's status, re-raising the signal that killed it.

A shell script can't set spawn attributes, and `open -a Sombra` (LaunchServices) would make Sombra responsible but loses the terminal's stdio, the exit status and the environment. A tiny native launcher is the standard answer. Models and config stay where the uv install keeps them (`~/.cache/sombra/models`, `~/.config/sombra`), because the app runs the same code with the same `HOME`.

### Entitlements (hardened runtime)

The app is signed with `--options runtime`, which notarization requires. It carries **one** entitlement:

| Entitlement | Why |
| --- | --- |
| `com.apple.security.device.audio-input` | Microphone (EU track). Under the hardened runtime, audio input is refused without it, even when the user granted Microphone. The Core Audio process tap goes through the same audio stack. |

Considered and left out, since nothing we run needs them. The rule: add one only with evidence that the app fails without it.

- `com.apple.security.cs.disable-library-validation`: every dylib and extension module in the bundle is signed by us (below), so library validation passes. Python.org's framework build needs it because pip installs wheels signed by others into it; our bundle is sealed.
- `com.apple.security.cs.allow-unsigned-executable-memory` / `allow-jit`: onnxruntime, whisper.cpp (Metal shaders compile in a system service, not in our process) and numpy generate no code. The libffi closures used by ctypes, cffi (sounddevice's stream callback) and pyobjc use trampoline tables on arm64 macOS, not writable+executable pages.
- `com.apple.security.cs.allow-dyld-environment-variables`: the bootloader finds its libraries with `@rpath`/`@loader_path`, not `DYLD_*`.
- `com.apple.security.automation.apple-events`: notifications go through `osascript display notification`, a separate process, and we send no Apple Events.
- Screen Recording and Accessibility have no entitlement; they are TCC grants only.

CI covers `--version`, `doctor` and `replay` (onnxruntime VAD, whisper.cpp, numpy, pyobjc imports) under the hardened runtime. The live-capture callbacks (PortAudio through cffi, the process tap, ScreenCaptureKit) need permissions a CI runner doesn't have. That is why a live meeting on a real Mac is in [Left for a human](#left-for-a-human), before any final release.

### Signing, inside-out

`packaging/macos/sign.py` signs every Mach-O in the bundle except the main executable and the shim, deepest first, batched per `codesign` call. Symlinks are skipped: their targets are signed where they live. It then signs any nested `.framework`/`.bundle` and the shim, with its own identifier `io.github.nickmaglowsch.Sombra.cli`. Last comes the bundle, with `entitlements.plist`, which signs the main executable and seals the resources. Every signature uses `--options runtime`, and `--timestamp` with a Developer ID (a secure timestamp is required for notarization). The script ends with `codesign --verify --deep --strict`. We don't use `codesign --deep` to sign, because Apple discourages it: it applies one set of options to everything.

Ad-hoc (`--identity -`) runs exactly the same code without the timestamp. That is what every PR builds and smoke-tests, so the release path differs only in the identity.

### Release pipeline (`.github/workflows/release.yml`)

- **app** (`macos-14`): builds `Sombra.app` from the wheel the `build` job made (`packaging/macos/build.py`: `uv export --frozen --group app` → clean venv → PyInstaller → localisations → shim). It checks the `Info.plist` version, then signs. With the secrets (tags and manual runs only) it imports the Developer ID Application certificate into a temporary keychain, signs, notarizes with `xcrun notarytool submit --wait` (App Store Connect API key), staples, and runs `stapler validate` and `spctl --assess --type execute`. Without them it signs ad-hoc. Either way it zips with `ditto -c -k --sequesterRsrc --keepParent`, reports sizes in the job summary and uploads the `macos-app` artifact.
- **app-smoke** (a clean `macos-14`): installs the zip with `install.sh --from-app-zip`, then runs `codesign --verify --deep --strict` and records the gate evidence (`app_gate.py record`: `codesign -dv`, the strict verify, `spctl --assess --type execute`, `stapler validate`, the zip's SHA-256, the bundle id and version). Through the shim it runs `sombra --version` (must print the tag's version), `sombra doctor --json` (the "permissions holder" check must name Sombra) and `sombra replay` on the e2e fixture with the fake brain and whisper tiny (exactly one trigger, suggestion and action). As a control, running the bootloader directly, without the shim, must **not** report Sombra as the holder.
- **publish** needs both. `app_gate.py decide` **refuses a final tag** (and so publishes nothing) unless the app is Developer ID signed, has the hardened runtime, passes the strict verify, is accepted by Gatekeeper as "Notarized Developer ID" (`spctl --assess --type execute` in app-smoke) and is stapled. With `APPLE_TEAM_ID` set, the team must match too. A pre-release without that publishes **without** the app, with a warning. A name, SHA-256, version or bundle-id mismatch between the verified zip and the one being published is refused for any tag. When the app passes, the zip is added to the assets and `SHA256SUMS` is rewritten.

### Release asset interface (for #51)

- `Sombra-<version>-macos-arm64.zip`: a `ditto` zip of `Sombra.app`, listed in `SHA256SUMS`. `<version>` is the PEP 440 version of the tag (`v0.2.0` → `0.2.0`, `v0.2.0-rc1` → `0.2.0rc1`), the same as the wheel's.
- Inside, `Sombra.app/Contents/Info.plist` has `CFBundleShortVersionString` = `<version>`, and the CLI shim is `Sombra.app/Contents/Helpers/sombra`.
- It is attached only to releases whose app passed the gate: always for a final release, and for a pre-release only when it was signed and notarized.

### Installer and `doctor`

- `install.sh` on macOS downloads the zip listed in `SHA256SUMS` (checksum verified) and unpacks it with `ditto`. It checks the bundle id and `codesign --verify --deep --strict` (refusing on failure) and warns when Gatekeeper rejects the app. It installs into `/Applications`, or `~/Applications` when `/Applications` isn't writable, upgrading an existing Sombra in place and never replacing another app. Then it links the shim to `~/.local/bin/sombra` and removes an older uv-tool install. A release with no app falls back to the uv install, and `--no-app` forces it. `--uninstall` removes the app and the link, and prints `tccutil reset All io.github.nickmaglowsch.Sombra` for the grants (macOS keeps them).
- `sombra doctor` has a **permissions holder** check. It asks libSystem for the responsible process (`responsibility_get_pid_responsible_for_pid` + `proc_pidpath`) and names its app (outermost `.app`, with `CFBundleName` and bundle id), for example `Sombra (io.github.nickmaglowsch.Sombra) at /Applications/Sombra.app`. The check is ok for Sombra, and ok with a note suggesting Sombra.app for a terminal. It warns when sombra runs from Sombra.app but something else is responsible (the shim was bypassed), and warns when the responsible process cannot be read. The permission checks name that same app in their fixes.

## Evidence from CI

Filled in from the PR run that built and smoke-tested the app (linked in the PR). Numbers are from that run.

<!-- CI-EVIDENCE -->

## Consequences

- macOS users get two install paths: the app (default) and the uv tool (`--no-app`, and the fallback). Both run the same code from the same wheel. Only the app keeps grants scoped to Sombra.
- The app is large: a full CPython, numpy, onnxruntime, whisper.cpp and pyobjc. That is the price of self-contained. Models are not in the bundle; they download to `~/.cache/sombra/models` as before.
- The disclaim SPI is private. If Apple removes it, the shim still runs Sombra, the grants fall back to the terminal, and `doctor` says so. It would not silently break.
- Every release needs a macOS runner and Apple's notarization queue (minutes, sometimes more; the job allows 90 minutes). A notarization failure fails the release, and nothing is published.
- PyInstaller joins the lock as a non-default dependency group (`app`); `uv sync` and `make check` don't install it.

## Left for a human

1. Add the Apple secrets (steps in [docs/release.md](../release.md#apple-signing-and-notarization-sombraapp)).
2. Run the Release workflow by hand (**Actions → Release → Run workflow**, on `main`) and check that the **app** job signs, notarizes and staples, and that **app-smoke** records `developer-id`, `Notarized Developer ID` and stapled. Then push an rc tag (`v0.2.0-rc1`) and check that the pre-release carries `Sombra-0.2.0rc1-macos-arm64.zip` in `SHA256SUMS`.
3. On a real Apple Silicon Mac with macOS 14+, install with `curl … install.sh | sh -s -- --version v0.2.0-rc1`. Run `sombra doctor` (permissions holder: Sombra), then `sombra start` for a short test meeting with a second device on the call. Accept the Microphone, System Audio Recording, Screen Recording and Accessibility prompts: **they must name Sombra**, and System Settings > Privacy & Security must list **Sombra**, not Terminal. Keep the `log stream` from [How TCC decides who asks](#how-tcc-decides-who-asks) running and save the `AttributionChain` lines. Confirm the transcript has both EU and OUTROS, and that screenshots were taken (this is the hardened-runtime check for the live-capture callbacks).
4. Upgrade: install the next rc and check that no prompt reappears and that System Settings still lists Sombra as allowed.
5. Report the results on #50 and move this ADR to **accepted**.
